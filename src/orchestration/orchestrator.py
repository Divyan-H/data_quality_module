"""Orchestration: the pipeline A -> B -> C -> D as a state machine over run folders.

    locked --(previous phase accepted)--> ready --start--> running --> review --accept--> accepted
                                                               |          |  ^
                                                               |          +--+ upload an edited workbook (re-checked)
                                                               +--> failed | cancelled   (start again)

The orchestrator owns `state.json`. Phase code runs in worker processes (`runner`); this module decides what each
outcome means: which phase unlocks, which downstream outputs are discarded because their input changed, and when the
analyst is asked about the LLM. HTTP knows nothing of this; it only calls the methods below.
"""
from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

from src.common import run_inputs
from src.llm_interface.ollama_status import probe
from src.orchestration import bundle
from src.orchestration.phases import ORDER, PHASES, following, previous
from src.orchestration.phases.base import Paths, PhaseError, read_json
from src.orchestration.phases.hygiene import fix_catalogue
from src.orchestration.runner import Busy, Job, JobRunner, kill_orphan
from src.orchestration.settings import ServiceSettings
from src.security.uploads import (DATA_EXTENSIONS, LEGACY_EXTENSIONS, WORKBOOK_EXTENSIONS, UploadRejected, safe_filename,
                                  safe_relative_path, unique_name, valid_artifact_id)
from src.structure.readers import find_libreoffice
from src.storage.run_store import RunNotFound, RunStore, now

LOCKED, READY, RUNNING, REVIEW, ACCEPTED, FAILED, CANCELLED = (
    "locked", "ready", "running", "review", "accepted", "failed", "cancelled")
RESTARTABLE = {READY, FAILED, CANCELLED}
SYNC_WAIT_S = 180                         # template creation and config checks are short jobs the request waits for


class PipelineError(Exception):
    """A refused request. `status` is the HTTP status; `code` is stable for the UI; `extra` is machine-readable."""

    def __init__(self, status: int, code: str, message: str, extra: dict | None = None):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra or {}


def _listed(artifacts: list) -> list[tuple]:
    """(artifact, size) for the files that exist. A running job may swap a phase's folder while the page polls,
    so a file can vanish between listing and reading it; it is simply left out of that answer."""
    out = []
    for a in artifacts:
        try:
            out.append((a, a.path.stat().st_size))
        except OSError:
            continue
    return out


def _blank_phase(key: str, status: str) -> dict:
    return {"key": key, "status": status, "attempt": 0, "params": {}, "started_at": None, "finished_at": None,
            "error": None, "upload_error": None, "uploads": [], "llm": None, "candidates": None,
            "skipped": False, "skip_reason": None,
            "config": {"uploaded": False, "valid": False, "name": None, "table": None, "preview": None, "error": None}}


class Pipeline:
    def __init__(self, settings: ServiceSettings, store: RunStore | None = None, runner: JobRunner | None = None):
        self.settings = settings
        self.store = store or RunStore(settings.workspace)
        self.runner = runner or JobRunner(self.store, settings.settings_path, self._finished,
                                          settings.max_concurrent_jobs, settings.job_timeout_min * 60)
        self._sync_locks: dict[str, threading.Lock] = {}
        self._recover()

    # ------------------------------------------------------------------ startup
    def _recover(self) -> None:
        """A run left 'running' by a previous service process has no worker any more: say so, and let it restart."""
        for st in self.store.list():
            job = st.get("job")
            if not job:
                continue
            kill_orphan(job.get("pid"))

            def fix(s: dict, job=job) -> None:
                ph = s["phases"][job["phase"]]
                if job["action"] == "run":
                    ph.update(status=FAILED, finished_at=now(),
                              error={"code": "interrupted", "message": "The service stopped while this phase was running. Start it again."})
                s["job"] = None
            self.store.update(st["id"], fix)

    # ------------------------------------------------------------------ runs
    def create_run(self, name: str, files: list[tuple[str, Path]], llm: str = "on") -> str:
        """files: (relative path, temporary file already on disk). They are moved into the new run's inputs/."""
        if not files:
            raise PipelineError(400, "no_files", "Choose at least one file.")
        total = 0
        taken: set[str] = set()
        checked = []
        try:
            for rel, tmp in files:
                base = safe_filename(Path(rel.replace("\\", "/")).name, DATA_EXTENSIONS)
                rel = unique_name(safe_relative_path(rel, base), taken)
                taken.add(rel)
                total += Path(tmp).stat().st_size
                checked.append((rel, Path(tmp)))
        except UploadRejected as e:
            raise PipelineError(400, "bad_file", str(e)) from e
        if total > self.settings.max_upload_mb * 1024 * 1024:
            raise PipelineError(413, "too_large", f"The files total {total / 2**20:.0f} MB; the limit is {self.settings.max_upload_mb} MB.")
        state = {"name": (name or checked[0][0]).strip()[:120], "options": {"llm": "on" if llm != "off" else "off"},
                 "inputs": [], "job": None,
                 "phases": {k: _blank_phase(k, READY if k == "A" else LOCKED) for k in ORDER}}
        run_id = self.store.create(state["name"], state)
        inputs = self.store.path(run_id) / "inputs"
        listed = []
        for rel, tmp in checked:
            dst = inputs / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(tmp), dst)
            listed.append({"path": rel, "bytes": dst.stat().st_size})
        self.store.update(run_id, lambda s: s.update(inputs=listed))
        return run_id

    def delete_run(self, run_id: str) -> None:
        if self.runner.active(run_id):
            self.runner.cancel(run_id)
        self.store.delete(run_id)

    def list_runs(self) -> list[dict]:
        out = []
        for s in self.store.list():
            phases = s["phases"]
            out.append({"id": s["id"], "name": s["name"], "created_at": s["created_at"], "updated_at": s["updated_at"],
                        "files": len(s["inputs"]), "running": bool(s.get("job")),
                        "phases": {k: phases[k]["status"] for k in ORDER},
                        "complete": phases["D"]["status"] == ACCEPTED})
        return out

    # ------------------------------------------------------------------ views
    def view(self, run_id: str) -> dict:
        st = self.store.load(run_id)
        run = self.store.path(run_id)
        phases = []
        for key in ORDER:
            adapter, ph = PHASES[key], st["phases"][key]
            arts = _listed(adapter.artifacts(run)) if ph["status"] in (REVIEW, ACCEPTED, RUNNING) else []
            ok, why = adapter.can_continue(run) if ph["status"] == REVIEW else (False, None)
            phases.append({**{k: v for k, v in ph.items() if not k.startswith("_")}, "title": adapter.title, "short": adapter.short, "describes": adapter.describes,
                           "uses_llm": adapter.uses_llm, "needs_config": adapter.needs_config,
                           "review_name": adapter.review_name, "can_continue": ok, "blocked_reason": why,
                           "has_summary": (run / "summaries" / f"{key}.json").exists() and ph["status"] in (REVIEW, ACCEPTED),
                           "artifacts": [{"id": a.id, "label": a.label, "kind": a.kind, "primary": a.primary,
                                          "description": a.description, "bytes": size} for a, size in arts]})
        job = st.get("job")
        return {"id": st["id"], "name": st["name"], "created_at": st["created_at"], "updated_at": st["updated_at"],
                "inputs": st["inputs"], "options": st["options"], "job": job, "phases": phases,
                "complete": st["phases"]["D"]["status"] == ACCEPTED}

    def summary(self, run_id: str, key: str) -> dict:
        self._phase(key)
        data = read_json(self.store.path(run_id) / "summaries" / f"{key}.json")
        if data is None:
            raise PipelineError(404, "no_summary", "This phase has no results yet.")
        return data

    def llm_status(self, fresh: bool = False) -> dict:
        return probe(self.settings.llm_model, fresh=fresh).as_dict()

    def options(self) -> dict:
        return {"llm": {"model": self.settings.llm_model, "review": self.settings.llm_review},
                "limits": {"max_upload_mb": self.settings.max_upload_mb,
                           "max_review_upload_mb": self.settings.max_review_upload_mb,
                           "extensions": sorted(DATA_EXTENSIONS),
                           # .xls / .xlsb / .ods are converted by LibreOffice (Phase A, spec 3.1); without it they cannot be read
                           "legacy_converter": find_libreoffice() is not None,
                           "legacy_extensions": sorted(LEGACY_EXTENSIONS)},
                "hygiene_fixes": fix_catalogue(set(self.settings.hygiene_defaults_off)),
                "phases": [{"key": k, "title": PHASES[k].title, "short": PHASES[k].short, "uses_llm": PHASES[k].uses_llm,
                            "needs_config": PHASES[k].needs_config} for k in ORDER]}

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _phase(key: str):
        if key not in PHASES:
            raise PipelineError(404, "unknown_phase", f"There is no phase {key}.")
        return PHASES[key]

    def _clear_from(self, st: dict, run: Path, key: str, include_self: bool) -> None:
        """Discard the outputs of `key` (optionally) and every later phase; they are locked until their input is accepted again."""
        keys = ([key] if include_self else []) + following(key)
        for k in keys:
            PHASES[k].clear(run)
            for sub in ("summaries",):
                (run / sub / f"{k}.json").unlink(missing_ok=True)
            ph = st["phases"][k]
            cfg = ph["config"]
            ph.update(status=LOCKED if k != key else ph["status"], error=None, upload_error=None, uploads=[],
                      started_at=None, finished_at=None, params={}, llm=None, candidates=None,
                      skipped=False, skip_reason=None)
            ph.pop("_reviewing", None)
            if k != key and cfg["uploaded"]:         # the data it described has changed: check it again before use
                cfg.update(valid=False, preview=None, error=None)
        shutil.rmtree(Paths(run).final, ignore_errors=True)
        Paths(run).final.mkdir(exist_ok=True)

    def _llm_params(self, adapter, options: dict, st: dict) -> tuple[dict | None, dict | None]:
        """(params['llm'], phase llm record). Raises PipelineError(409, llm_unavailable) when the LLM is wanted but down."""
        if not adapter.uses_llm:
            return None, None
        want = options.get("llm") or st["options"]["llm"]
        if want == "off":
            return ({"mode": "off"}, {"requested": "off", "used": False, "model": None, "calls": 0,
                                      "reason": "Continued without the LLM (rule-based fallback), as chosen."})
        s = probe(self.settings.llm_model, fresh=True)
        if not s.available:
            raise PipelineError(409, "llm_unavailable", s.message, {"llm": s.as_dict()})
        review = options.get("llm_review") or self.settings.llm_review
        if review not in ("near_ties", "all"):
            raise PipelineError(400, "bad_option", "llm_review must be near_ties or all")
        return ({"mode": "assist", "model": s.model, "digest": s.digest, "review": review, "privacy": "full", "votes": 3},
                {"requested": "on", "used": None, "model": s.model, "calls": None, "reason": None})

    # ------------------------------------------------------------------ actions
    def start(self, run_id: str, key: str, options: dict | None = None) -> dict:
        options = options or {}
        adapter = self._phase(key)
        run = self.store.path(run_id)
        llm_params, llm_record = self._llm_params(adapter, options, self.store.load(run_id))
        with self.store.edit(run_id) as st:
            ph = st["phases"][key]
            if st.get("job"):
                raise PipelineError(409, "busy", "This run already has a job in progress.")
            prev = previous(key)
            if prev and st["phases"][prev]["status"] != ACCEPTED:
                raise PipelineError(409, "locked", f"Accept Phase {prev} before starting Phase {key}.")
            if ph["status"] in (REVIEW, ACCEPTED) and not options.get("restart"):
                raise PipelineError(409, "restart_required", f"Phase {key} already has results; starting again discards them and every later phase.")
            if adapter.needs_config and not ph["config"]["valid"]:
                raise PipelineError(409, "config_required", f"Upload a valid client config workbook for Phase {key} first.")
            params: dict = {}
            if llm_params:
                params["llm"] = llm_params
            if key == "B":
                fixes = {f["id"]: f["default"] for f in fix_catalogue(set(self.settings.hygiene_defaults_off))}
                fixes.update({k: bool(v) for k, v in (options.get("fixes") or {}).items()})
                params["fixes"] = fixes
                if options.get("zero_expected"):
                    params["zero_expected"] = options["zero_expected"]
            if adapter.needs_config:
                params["table"] = ph["config"]["table"]
            self._clear_from(st, run, key, include_self=True)
            try:
                job = self.runner.launch(run_id, key, "run", params)
            except Busy as e:
                raise PipelineError(429, "busy", str(e)) from e
            ph.update(status=RUNNING, attempt=ph["attempt"] + 1, params=params, started_at=now(), finished_at=None,
                      error=None, upload_error=None, llm=llm_record)
            st["job"] = {"id": job.id, "phase": key, "action": "run", "started_at": now(), "pid": job.proc.pid}
        return self.view(run_id)

    def cancel(self, run_id: str) -> dict:
        if not self.runner.cancel(run_id):
            raise PipelineError(409, "nothing_running", "No job is running for this run.")
        job = self.runner.active(run_id)
        if job:
            job.done.wait(15)
        return self.view(run_id)

    def accept(self, run_id: str, key: str) -> dict:
        adapter = self._phase(key)
        run = self.store.path(run_id)
        with self.store.edit(run_id) as st:
            ph = st["phases"][key]
            if st.get("job"):
                raise PipelineError(409, "busy", "A job is in progress.")
            if ph["status"] == ACCEPTED:
                return self.view(run_id)
            if ph["status"] != REVIEW:
                raise PipelineError(409, "not_reviewable", f"Phase {key} has no finished results to accept.")
            ok, why = adapter.can_continue(run)
            if not ok:
                raise PipelineError(409, "cannot_continue", why or "This phase cannot be accepted yet.")
            ph["status"] = ACCEPTED
            nxt = following(key)
            if nxt and st["phases"][nxt[0]]["status"] == LOCKED:
                st["phases"][nxt[0]]["status"] = READY
        return self.view(run_id)

    def select_tables(self, run_id: str, refs: list[str]) -> dict:
        """Keep only `refs` of Phase A's tables for the next phases. Later phases that already ran on another choice
        are discarded (they are locked again until Phase A is accepted, or become ready when it already is)."""
        adapter = PHASES["A"]
        run = self.store.path(run_id)
        with self.store.edit(run_id) as st:
            ph = st["phases"]["A"]
            if st.get("job"):
                raise PipelineError(409, "busy", "A job is in progress.")
            if ph["status"] not in (REVIEW, ACCEPTED):
                raise PipelineError(409, "not_reviewable", "Phase A has no tables to choose from yet.")
            try:
                adapter.select(run, refs)
            except PhaseError as e:
                raise PipelineError(400, e.code, e.message) from e
            (run / "summaries").mkdir(exist_ok=True)                  # the stored summary still shows the old ticks otherwise
            (run / "summaries" / "A.json").write_text(json.dumps(adapter.summary(run), ensure_ascii=False, default=str), encoding="utf8")
            self._clear_from(st, run, "A", include_self=False)
            if ph["status"] == ACCEPTED:
                st["phases"]["B"]["status"] = READY
        return self.view(run_id)

    def upload_review(self, run_id: str, key: str, src: Path, filename: str, options: dict | None = None) -> dict:
        """Check and apply an edited review workbook. Returns at once; the outcome arrives through the run's state."""
        options = options or {}
        adapter = self._phase(key)
        run = self.store.path(run_id)
        try:
            name = safe_filename(filename, WORKBOOK_EXTENSIONS)
        except UploadRejected as e:
            raise PipelineError(400, "bad_file", str(e)) from e
        if Path(src).stat().st_size > self.settings.max_review_upload_mb * 1024 * 1024:
            raise PipelineError(413, "too_large", f"The workbook is larger than {self.settings.max_review_upload_mb} MB.")
        with self.store.edit(run_id) as st:
            ph = st["phases"][key]
            if st.get("job"):
                raise PipelineError(409, "busy", "A job is in progress.")
            if ph["status"] not in (REVIEW, ACCEPTED):
                raise PipelineError(409, "not_reviewable", f"Phase {key} has no results to review yet.")
            params = dict(ph["params"])
            if adapter.uses_llm and (params.get("llm") or {}).get("mode") == "assist":
                if options.get("llm") == "off":
                    params["llm"] = {"mode": "off"}
                else:
                    s = probe(self.settings.llm_model, fresh=True)
                    if not s.available:
                        raise PipelineError(409, "llm_unavailable", s.message, {"llm": s.as_dict()})
            dst = run / "uploads" / f"incoming_{key}_{name}"
            dst.parent.mkdir(exist_ok=True)
            shutil.copy(src, dst)
            try:
                job = self.runner.launch(run_id, key, "review", params, upload=dst)
            except Busy as e:
                raise PipelineError(429, "busy", str(e)) from e
            ph["upload_error"] = None
            ph["_reviewing"] = {"name": name, "was": ph["status"]}
            ph["status"] = RUNNING
            st["job"] = {"id": job.id, "phase": key, "action": "review", "started_at": now(), "pid": job.proc.pid}
        return self.view(run_id)

    # -- client config workbooks (C and D) -----------------------------------
    def _sync_job(self, run_id: str, key: str, action: str, params: dict, upload: Path | None = None) -> dict:
        """Short config-step jobs (table list, template, config check) take turns: a request that arrives while another
        one runs waits for it instead of failing, because each can take tens of seconds on a large table."""
        with self._sync_locks.setdefault(run_id, threading.Lock()):
            return self._run_sync_job(run_id, key, action, params, upload)

    def _run_sync_job(self, run_id: str, key: str, action: str, params: dict, upload: Path | None) -> dict:
        with self.store.edit(run_id) as st:
            if st.get("job"):
                raise PipelineError(409, "busy", "A job is in progress.")
            try:
                job = self.runner.launch(run_id, key, action, params, upload=upload)
            except Busy as e:
                raise PipelineError(429, "busy", str(e)) from e
            st["job"] = {"id": job.id, "phase": key, "action": action, "started_at": now(), "pid": job.proc.pid}
        outcome = self.runner.wait(job, SYNC_WAIT_S)
        if outcome is None:
            self.runner.cancel(run_id)
            raise PipelineError(504, "timeout", "The step took too long.")
        return outcome

    def skip(self, run_id: str, key: str, reason: str | None = None) -> dict:
        """Move on without running a config phase (C or D): for data the phase does not apply to, or when the analyst
        chooses to. The phase counts as accepted so the next one unlocks; it is marked skipped everywhere it shows."""
        adapter = self._phase(key)
        if not adapter.needs_config:
            raise PipelineError(400, "not_skippable", f"Phase {key} cannot be skipped: later phases need its output.")
        run = self.store.path(run_id)
        with self.store.edit(run_id) as st:
            ph = st["phases"][key]
            if st.get("job"):
                raise PipelineError(409, "busy", "A job is in progress.")
            prev = previous(key)
            if prev and st["phases"][prev]["status"] != ACCEPTED:
                raise PipelineError(409, "locked", f"Accept Phase {prev} first.")
            if ph["status"] in (REVIEW, ACCEPTED) and not ph.get("skipped"):
                raise PipelineError(409, "has_results", f"Phase {key} already ran; run it again or accept it instead of skipping.")
            self._clear_from(st, run, key, include_self=True)
            ph.update(status=ACCEPTED, skipped=True, finished_at=now(),
                      skip_reason=(reason or "Skipped by the analyst.")[:300])
            nxt = following(key)
            if nxt and st["phases"][nxt[0]]["status"] == LOCKED:
                st["phases"][nxt[0]]["status"] = READY
        return self.view(run_id)

    def _config_phase(self, run_id: str, key: str):
        adapter = self._phase(key)
        if not adapter.needs_config:
            raise PipelineError(400, "no_config", f"Phase {key} does not use a client config workbook.")
        st = self.store.load(run_id)
        prev = previous(key)
        if prev and st["phases"][prev]["status"] != ACCEPTED:
            raise PipelineError(409, "locked", f"Accept Phase {prev} first.")
        return adapter

    def columns(self, run_id: str, key: str) -> list[dict]:
        """Numeric columns of the tables a phase will clean, for the start-screen choice of where zero is a valid value."""
        adapter = self._phase(key)
        if key != "B":
            raise PipelineError(400, "no_columns", f"Phase {key} has no column choices.")
        st = self.store.load(run_id)
        if st["phases"]["A"]["status"] != ACCEPTED:
            raise PipelineError(409, "locked", "Accept Phase A first.")
        outcome = self._sync_job(run_id, key, "columns", {})
        if not outcome["ok"]:
            raise PipelineError(422, outcome["code"], outcome["message"])
        return outcome["data"]["tables"]

    def get_inputs(self, run_id: str) -> dict:
        """What the project told us about this run (columns to expect and check, city list, currency), plus the tables and
        columns the answers can refer to once Phase A is accepted. `source` is "run" once the run has its own answers."""
        run = self.store.path(run_id)
        inputs, source = run_inputs.load_for_run(run)
        path = run_inputs.run_path(run)
        text = path.read_text(encoding="utf8") if source == "run" else run_inputs.load_text(run_inputs.DEFAULT_PATH)
        tables: list[dict] = []
        if self.store.load(run_id)["phases"]["A"]["status"] == ACCEPTED:
            try:
                tables = [{"ref": t["ref"], "file": t["file"], "sheet": t["sheet"], "rows": t["rows"], "columns": t.get("column_names", [])}
                          for t in self.columns(run_id, "B")]
            except PipelineError:
                tables = []
        return {"source": source, "asked": source == "run", "inputs": inputs, "yaml": text, "tables": tables}

    def set_inputs(self, run_id: str, raw) -> dict:
        """Validate and store the run's inputs. They are read when a phase starts, so a phase that already ran keeps its results."""
        run = self.store.path(run_id)
        _, problems = run_inputs.save_for_run(run, raw)
        if problems:
            raise PipelineError(422, "inputs_invalid", "The inputs were not accepted; nothing was saved.",
                                {"problems": [{"message": p} for p in problems]})
        return self.get_inputs(run_id)

    def candidates(self, run_id: str, key: str) -> list[dict]:
        self._config_phase(run_id, key)
        cached = self.store.load(run_id)["phases"][key].get("candidates")
        if cached is not None:                                   # profiling a large table takes seconds: do it once
            return cached
        outcome = self._sync_job(run_id, key, "candidates", {})
        if not outcome["ok"]:
            raise PipelineError(422, outcome["code"], outcome["message"])
        tables = outcome["data"]["tables"]
        self.store.update(run_id, lambda s: s["phases"][key].update(candidates=tables))
        return tables

    def config_template(self, run_id: str, key: str, table: str | None) -> tuple[Path, str]:
        self._config_phase(run_id, key)
        outcome = self._sync_job(run_id, key, "template", {"table": table})
        if not outcome["ok"]:
            raise PipelineError(422, outcome["code"], outcome["message"])
        path = Path(outcome["data"]["path"])
        return path, path.name

    def upload_config(self, run_id: str, key: str, src: Path | None, filename: str | None, table: str | None) -> dict:
        """Validate a filled client config workbook against the data. src=None re-checks the one already stored."""
        self._config_phase(run_id, key)
        run = self.store.path(run_id)
        stored = run / "config" / f"phase_{key}_client_config.xlsx"
        if src is not None:
            try:
                safe_filename(filename, WORKBOOK_EXTENSIONS)
            except UploadRejected as e:
                raise PipelineError(400, "bad_file", str(e)) from e
            if Path(src).stat().st_size > self.settings.max_review_upload_mb * 1024 * 1024:
                raise PipelineError(413, "too_large", "The workbook is too large.")
            candidate = run / "config" / f"incoming_{key}.xlsx"
            shutil.copy(src, candidate)
        elif stored.exists():
            candidate = stored
        else:
            raise PipelineError(409, "config_missing", "No client config workbook has been uploaded yet.")
        outcome = self._sync_job(run_id, key, "check_config", {"table": table}, upload=candidate)
        if outcome["ok"] and candidate != stored:
            shutil.move(str(candidate), stored)
        elif candidate != stored:
            candidate.unlink(missing_ok=True)

        def save(st: dict) -> None:
            cfg = st["phases"][key]["config"]
            if outcome["ok"]:
                cfg.update(uploaded=True, valid=True, name=filename or cfg["name"], table=outcome["data"].get("table"),
                           preview=outcome["data"], error=None)
            else:
                cfg.update(valid=False, preview=None, error={"message": outcome["message"], "problems": outcome.get("problems", [])})
                if src is not None:
                    cfg.update(uploaded=stored.exists())
        self.store.update(run_id, save)
        if not outcome["ok"]:
            raise PipelineError(422, outcome.get("code", "config_invalid"), outcome["message"],
                                {"problems": outcome.get("problems", [])})
        return self.view(run_id)

    # ------------------------------------------------------------------ job outcomes
    def _finished(self, job: Job, outcome: dict) -> None:
        """Runs in the runner's watcher thread when a worker ends."""
        def apply(st: dict) -> None:
            st["job"] = None
            ph = st["phases"][job.phase]
            run = self.store.path(job.run_id)
            if job.action == "run":
                if outcome["ok"]:
                    ph.update(status=REVIEW, finished_at=now(), error=None)
                    self._note_llm(ph, outcome, job)
                else:
                    ph.update(status=CANCELLED if outcome["kind"] == "cancelled" else FAILED, finished_at=now(),
                              error={"code": outcome.get("code"), "message": outcome["message"]})
                    self._note_llm(ph, outcome, job)
            elif job.action == "review":
                was = (ph.pop("_reviewing", None) or {})
                if outcome["ok"]:
                    ph["status"] = REVIEW                    # results changed: look again, accept again
                    ph["uploads"].append({"name": was.get("name"), "at": now(), "ok": True,
                                          "applied": outcome.get("data", {}).get("applied")})
                    ph["error"] = None
                    self._clear_from(st, run, job.phase, include_self=False)      # later phases saw the old data
                else:
                    ph["status"] = was.get("was", REVIEW) if outcome["kind"] != "crash" else FAILED
                    ph["upload_error"] = {"code": outcome.get("code"), "message": outcome["message"],
                                          "problems": outcome.get("problems", []), "at": now()}
                    ph["uploads"].append({"name": was.get("name"), "at": now(), "ok": False})
                    if outcome["kind"] == "crash":
                        ph["error"] = {"code": outcome.get("code"), "message": outcome["message"]}
        try:
            self.store.update(job.run_id, apply)
        except RunNotFound:
            return                                           # the run was deleted while the job ran

    @staticmethod
    def _note_llm(ph: dict, outcome: dict, job: Job) -> None:
        rec = ph.get("llm")
        if not rec or rec.get("requested") != "on":
            return
        calls = outcome.get("llm_calls")
        rec["calls"] = calls
        rec["used"] = bool(calls)
        if not calls and rec["reason"] is None:
            rec["reason"] = "The model was not called."

    # ------------------------------------------------------------------ files
    def artifact(self, run_id: str, artifact_id: str) -> tuple[Path, str]:
        valid_artifact_id(artifact_id)
        run = self.store.path(run_id)
        for a in bundle.collect(run):
            if a.id == artifact_id and a.path.is_file():
                return a.path, a.label
        raise PipelineError(404, "no_such_file", "That file does not exist (yet).")

    def results(self, run_id: str) -> dict:
        run = self.store.path(run_id)
        st = self.store.load(run_id)
        arts = _listed(bundle.collect(run))
        return {"complete": st["phases"]["D"]["status"] == ACCEPTED,
                "files": [{"id": a.id, "label": a.label, "group": bundle.group_of(a), "phase": a.phase, "kind": a.kind,
                           "description": a.description, "bytes": size} for a, size in arts],
                "phases": {k: st["phases"][k]["status"] for k in ORDER}}

    def bundle_zip(self, run_id: str) -> tuple[Path, str]:
        run = self.store.path(run_id)
        st = self.store.load(run_id)
        path = bundle.build_zip(run, st["name"])
        return path, "data_quality_results.zip"

    def events(self, run_id: str, after: int) -> dict:
        items, cursor = self.store.events(run_id, after)
        return {"events": items, "cursor": cursor}

    def shutdown(self) -> None:
        self.runner.shutdown()
