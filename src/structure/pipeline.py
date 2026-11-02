"""data.structure job core: scan -> read -> regions -> tables -> findings -> outputs (spec 2.2, 4, 6, 11).

Runs against local paths. `job.py` maps Job Contract v1 (storage refs, events) onto `run_structure`.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from . import features, findings as fnd, selection
from .config import PIPELINE, RULE_PACK, ConfigInvalid, Settings, file_config_hash, resolve_settings
from .errors import Cancelled, FileUnreadable, JobFailed
from .extract import InvariantViolation, analyze_sheet
from .facts import sheet_facts, sheet_json
from .header import build_names
from .llm.client import OllamaClient
from .llm.examples import Library
from .llm.review import LLMConfig, Reviewer, project_hash_key
from .model import col_letter
from .review_workbook import export_review
from .readers import convert_to_xlsx, read_parquet_footer, read_text
from .sniff import sha256_file, sniff
from .writer import dump_json, hash_tree, write_audit, write_cells, write_table
from .xlsx import read_xlsx

EXT_FORMAT = {".csv": "text", ".tsv": "text", ".txt": "text", ".xlsx": "xlsx", ".xlsm": "xlsm",
              ".xls": "xls", ".xlsb": "xlsb", ".ods": "ods", ".parquet": "parquet"}
PIPELINE_MINOR = PIPELINE.rsplit(".", 1)[0]            # reuse needs the same rule pack and pipeline minor
# The hash key is a service secret (STRUCTURE_HASH_KEY, spec 10.8). The fallback only serves local runs.
DEV_HASH_SECRET = "structure-local-development-only"


@dataclass
class FileResult:
    file_key: str
    sha: str
    format: str
    size: int
    facts: dict = field(default_factory=dict)          # file.json: issues, names, sheets (value-free)
    converted_from: str | None = None
    macro_enabled: bool = False
    parquet: dict | None = None
    parquet_path: str | None = None
    parquet_names: list = field(default_factory=list)
    reused_from: str | None = None
    config_hash: str = ""

    @property
    def issues(self) -> list:
        return self.facts.get("issues", [])

    @property
    def blocked(self) -> bool:
        return any(sev == "block" for _, sev, _ in self.issues)

    @property
    def sheets(self) -> list:
        return self.facts.get("sheets", [])


class Progress:
    """Progress, log and cancellation hooks supplied by the caller (events of spec 11.3)."""

    def __init__(self, on_progress=None, on_log=None, is_cancelled=None):
        self._p, self._l, self._c = on_progress, on_log, is_cancelled

    def stage(self, name: str, pct: float | None, detail: str | None = None) -> None:
        if self._p:
            self._p(name, pct, detail)

    def log(self, message: str) -> None:          # names, coordinates and counts only, never a value (P7)
        if self._l:
            self._l(message)

    def check(self) -> None:
        if self._c and self._c():
            raise Cancelled()


# ------------------------------------------------------------------ one file


def _new_facts(issues: list) -> dict:
    return {"issues": issues, "names": [], "calc_on_load": False, "structure_locked": False, "external_links": 0,
            "sheets": []}


def process_file(entry: dict, sha: str, key: str, st: Settings, rv: Reviewer | None, scratch: str, out: Path,
                 edits: list[dict], progress: Progress) -> FileResult:
    """Read one file and write its tables under files/{key}/. Problems become findings, never exceptions."""
    path = entry["path"]
    sn = sniff(path)
    fr = FileResult(key, sha, sn.format, os.path.getsize(path), macro_enabled=sn.macro)
    fr.facts = _new_facts([])
    issues = fr.facts["issues"]
    ext_fmt = EXT_FORMAT.get(os.path.splitext(path)[1].lower())
    blockers = {"unreadable": ("file_unreadable", "The file is a corrupt package."),
                "empty": ("file_empty", "The file is empty (0 bytes)."),
                "password": ("password_protected", "The workbook is password protected. Remove the password and upload again."),
                "unsupported": ("unsupported_format", f"Unsupported file content ({sn.detail or 'unknown'})."
                                + (" Upload the folder, or the files, instead." if sn.detail == "archive" else ""))}
    if sn.format in blockers:
        issues.append([blockers[sn.format][0], "block", blockers[sn.format][1]])
        return fr
    if ext_fmt and ext_fmt != sn.format and {ext_fmt, sn.format} != {"xlsx", "xlsm"}:
        issues.append(["file_format_mismatch", "info",
                       f"The extension says {ext_fmt} but the content is {sn.format}; the content was used."])
    if sn.format == "parquet":
        return _parquet_file(fr, path, out)
    try:
        progress.stage("reading facts", 0)
        if sn.format == "text":
            read_opts = next((r for r in st.read if r.get("file") and _matches(r["file"], entry)), {})
            wb, info = read_text(path, read_opts)
            if info["confidence"] is not None:
                issues.append(["csv_encoding_guessed", "info",
                               f"Encoding guessed as {info['encoding']} (confidence {info['confidence']})."])
        else:
            src = path
            if sn.format in ("xls", "xlsb", "ods"):
                progress.stage("converting", 0)
                src = convert_to_xlsx(path, scratch)
                fr.converted_from = sn.format
                issues.append(["format_converted", "info",
                               f"Converted from {sn.format} to xlsx before reading; conditional formats, validations "
                               f"and drawings may be approximated."])
            def on_rows(sheet: str, row: int, last: int | None) -> None:
                progress.check()                          # a long read can be cancelled part-way
                progress.stage("reading facts", min(99.0, 100 * row / last) if last else None, f"{sheet}: {row:,} rows")

            wb = read_xlsx(src, macro=sn.macro, on_rows=on_rows)
    except FileUnreadable as e:
        issues.append(["file_unreadable", "block", str(e)])
        return fr
    if all(not sh.cells for sh in wb.sheets) and not any(sh.error for sh in wb.sheets):
        issues.append(["file_empty", "block", "The file has no populated cell."])
        return fr
    fr.facts.update(calc_on_load=wb.calc_on_load, structure_locked=wb.structure_locked,
                    external_links=wb.external_links, names=features.classify_names(wb))
    by_name = {s.name: s for s in wb.sheets}
    results = []
    overrides = [o for o in st.overrides if o["table"].startswith(key + ":")]
    for sh in wb.sheets:
        progress.check()                                     # cancellation is honoured between sheets
        progress.stage("finding regions", 0)
        try:
            res = analyze_sheet(sh, st.params, key, rv, overrides, edits)
        except InvariantViolation as e:
            raise JobFailed("invariant_violation", str(e)) from e
        features.annotate(res, by_name)
        results.append(res)
    if sn.format == "text" and wb.sheets[0].ragged_long:
        issues.append(["csv_ragged", "warn", f"{wb.sheets[0].ragged_long} record(s) have more fields than usual; "
                       f"extra fields were kept as unlabelled columns."])
    progress.stage("writing tables", 0)
    fdir = out / "files" / key
    fdir.mkdir(parents=True, exist_ok=True)                # a file may have no table at all
    for res in results:
        sh = res.sheet
        facts = sheet_facts(res)
        for tf, t in zip(facts["tables"], res.tables):
            tf["outputs"] = write_table(fdir / "tables" / f"s{sh.index:02d}_{t.key}", t, sh, key,
                                        st.version, RULE_PACK)
        fr.facts["sheets"].append(facts)            # sheets/NN.json is written in _finalize, once findings exist
    write_cells(fdir / "cells.parquet", results)
    write_audit(fdir / "audit.parquet", results, key)
    return fr


def _matches(pattern: str, entry: dict) -> bool:
    from fnmatch import fnmatch
    return fnmatch(entry.get("relative_path") or "", pattern)


def _parquet_file(fr: FileResult, path: str, out: Path) -> FileResult:
    """Parquet skips Structure: listed from its footer, passed on unchanged (spec 4.3)."""
    try:
        fr.parquet = read_parquet_footer(path)
    except FileUnreadable as e:
        fr.facts["issues"].append(["file_unreadable", "block", str(e)])
        return fr
    fr.parquet_path = path
    cols = [{"label": c["name"], "letter": col_letter(i + 1), "groups": []} for i, c in enumerate(fr.parquet["columns"])]
    build_names(cols, "_")
    fr.parquet_names = [c["name"] for c in cols]
    tdir = out / "files" / fr.file_key / "tables" / "s00_t1"
    ref = f"{fr.file_key}:0:t1"
    columns = [{"name": c["name"], "label": c["label"], "letter": c["letter"], "position": i + 1, "groups": [],
                "unit": None, "hidden": False, "outline_level": 0,
                "source_type": {"dominant": fr.parquet["columns"][i]["type"], "histogram": {}, "number_format": None},
                "profile": {}, "flags": []} for i, c in enumerate(cols)]
    dump_json(tdir / "columns.json", {"schema": "dp.structure.columns/1", "table_ref": ref, "columns": columns,
                                      "dropped": [], "data_ref": path, "rows": fr.parquet["rows"]})
    dump_json(tdir / "samples.json", {"schema": "dp.structure.samples/1", "table_ref": ref, "samples": {
        c["name"]: fr.parquet["samples"][c["name"]] for c in fr.parquet["columns"]}})
    return fr


def _write_file_json(out: Path, fr: FileResult) -> None:
    dump_json(out / "files" / fr.file_key / "file.json", {
        "schema": "dp.structure.file/1", "file_key": fr.file_key, "format": fr.format,
        "converted_from": fr.converted_from, "macro_enabled": fr.macro_enabled, "size_bytes": fr.size,
        "parquet": ({"rows": fr.parquet["rows"], "columns": fr.parquet["columns"], "names": fr.parquet_names,
                     "data_ref": fr.parquet_path} if fr.parquet else None),
        **fr.facts})


def _load_file_json(folder: Path) -> FileResult:
    d = json.loads((folder / "file.json").read_text(encoding="utf8"))
    fr = FileResult(d["file_key"], "", d["format"], d["size_bytes"], converted_from=d["converted_from"],
                    macro_enabled=d["macro_enabled"])
    fr.facts = {k: d[k] for k in ("issues", "names", "calc_on_load", "structure_locked", "sheets")}
    fr.facts["external_links"] = d.get("external_links", 0)
    if d.get("parquet"):
        p = d["parquet"]
        fr.parquet, fr.parquet_names, fr.parquet_path = {"rows": p["rows"], "columns": p["columns"]}, p["names"], p["data_ref"]
    return fr


# ------------------------------------------------------------------ reuse (spec 11.5)


def _previous(prev_dir: Path | None) -> dict:
    """Previous output: inventory files by content hash, and its decisions and job id."""
    if not prev_dir or not (prev_dir / "inventory.json").exists():
        return {"files": {}, "decisions": [], "job": None, "dir": None}
    inv = json.loads((prev_dir / "inventory.json").read_text(encoding="utf8"))
    dec_path = prev_dir / "llm" / "decisions.jsonl"
    decisions = [json.loads(x) for x in dec_path.read_text(encoding="utf8").splitlines() if x.strip()] \
        if dec_path.exists() else []
    return {"files": {f["content_hash"].split(":")[-1]: f for f in inv["files"]}, "decisions": decisions,
            "job": inv.get("job_id"), "dir": prev_dir}


def _reusable(prev: dict, sha: str, cfg_hash: str, llm_pin: dict, full: bool) -> dict | None:
    rec = prev["files"].get(sha)
    if full or rec is None:
        return None
    ok = (rec.get("config_hash") == cfg_hash and rec.get("rule_pack") == RULE_PACK
          and rec.get("pipeline_minor") == PIPELINE_MINOR and rec.get("llm_pin") == llm_pin)
    return rec if ok and prev["dir"] and (prev["dir"] / "files" / rec["file_key"]).exists() else None


# ------------------------------------------------------------------ job


def _reviewer(job: dict, st: Settings, prev: dict, library: Library) -> Reviewer | None:
    pin = job.get("llm") or {}
    if st.llm["mode"] == "off" or not pin.get("model"):
        return None                        # no model pinned on the run: nothing to ask, the rules decide alone
    cfg = LLMConfig(mode=st.llm["mode"], privacy=st.llm["privacy"], votes=st.llm["votes"], review=st.llm["review"],
                    model=pin.get("model", ""), digest=pin.get("model_digest"),
                    prompt_version=pin.get("prompt_version") or LLMConfig.prompt_version)
    client = OllamaClient(job.get("llm_url"), cfg.model, cfg.digest, transport=job.get("llm_transport")) if cfg.model else None
    key = project_hash_key(os.environ.get("STRUCTURE_HASH_KEY", DEV_HASH_SECRET), job.get("project_id", "local"))
    return Reviewer(cfg, st.params, client, key, prev["decisions"], prev["job"], library)


PARALLEL_FILES = 4                  # spec §4: up to 4 files per job in parallel
PARALLEL_MIN_BYTES = 5 << 20        # below this, starting worker processes costs more than it saves


def _process_all(todo: list[tuple], st: Settings, rv, scratch: str, out: Path, progress: Progress) -> dict:
    """Read every file that is not reused. With the LLM on, files run one after another: its questions must go
    through one queue in a fixed order (spec §5.10). Rules-only runs use up to 4 worker processes; each file
    writes only its own folder, so the output is identical either way (P6)."""
    total = sum(os.path.getsize(e["path"]) for e, *_ in todo)
    if rv is not None or len(todo) < 2 or total < PARALLEL_MIN_BYTES:
        done = {}
        for e, key, _cfg, file_edits in todo:
            progress.check()
            done[e["_sha"]] = process_file(e, e["_sha"], key, st, rv, scratch, out, file_edits, progress)
        return done
    from concurrent.futures import FIRST_EXCEPTION, ProcessPoolExecutor, wait
    with ProcessPoolExecutor(max_workers=min(PARALLEL_FILES, len(todo))) as pool:
        futures = {pool.submit(process_file, e, e["_sha"], key, st, None, scratch, out, file_edits, Progress()): e["_sha"]
                   for e, key, _cfg, file_edits in todo}
        pending = set(futures)
        while pending:
            finished, pending = wait(pending, timeout=5, return_when=FIRST_EXCEPTION)
            for f in finished:
                f.result()                                   # re-raises JobFailed from a worker
            try:
                progress.check()
            except Cancelled:
                for f in pending:
                    f.cancel()
                raise
        return {futures[f]: f.result() for f in futures}


def run_structure(job: dict) -> dict:
    """job: files [{path, source_id, file_id, relative_path, content_hash?}], config, output_dir and optionally
    previous_dir, examples_path, full, llm {model, model_digest, prompt_version}, llm_url, project_id, job_id,
    edits, progress (Progress)."""
    progress: Progress = job.get("progress") or Progress()
    try:
        st = resolve_settings(job.get("config"))
    except ConfigInvalid as e:
        raise JobFailed("config_invalid", str(e)) from e
    if job.get("settings_patch"):
        st = job["settings_patch"](st)                       # review import: patched selection and overrides
    out = Path(job["output_dir"])
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)

    progress.stage("scanning", 0)
    entries = sorted(job["files"], key=lambda f: (f["source_id"], f.get("relative_path") or f["path"]))
    for e in entries:
        if not os.path.exists(e["path"]):
            raise JobFailed("input_not_found", f"{e.get('relative_path') or e['path']} is missing", retryable=True)
        e["_sha"] = sha256_file(e["path"])
        if e.get("content_hash") and e["content_hash"].split(":")[-1] != e["_sha"]:
            raise JobFailed("input_hash_mismatch", f"{e.get('relative_path') or e['path']} changed after it was registered")
    shas = sorted({e["_sha"] for e in entries})
    klen = 16 if len({s[:12] for s in shas}) < len(shas) else 12
    keys = {s: s[:klen] for s in shas}

    prev = _previous(Path(job["previous_dir"]) if job.get("previous_dir") else None)
    library = Library.load(job.get("examples_path"))
    rv = _reviewer(job, st, prev, library)
    llm_pin = {**st.llm, **({"model": rv.cfg.model, "digest": rv.cfg.digest, "prompt": rv.cfg.prompt_version} if rv else {})}
    edits = job.get("edits") or []

    scratch = tempfile.mkdtemp(prefix="dp_structure_")
    processed: dict[str, FileResult] = {}
    todo: list[tuple] = []                                   # (entry, key, cfg_hash, file edits) to read afresh
    cfg_hashes: dict[str, str] = {}
    try:
        for n, e in enumerate(entries):
            progress.check()
            if e["_sha"] in processed or e["_sha"] in cfg_hashes:
                continue                                      # the same content is processed once
            key = keys[e["_sha"]]
            paths = [x.get("relative_path") or "" for x in entries if x["_sha"] == e["_sha"]]
            file_edits = [x for x in edits if x["row_id"].startswith(key + ":")]
            cfg_hash = file_config_hash(st, key, paths, [f"{x['row_id']}|{x['column']}|{x.get('to')}" for x in file_edits])
            cfg_hashes[e["_sha"]] = cfg_hash
            progress.stage("scanning", 100 * n / max(len(entries), 1))
            rec = _reusable(prev, e["_sha"], cfg_hash, llm_pin, bool(job.get("full")))
            if rec:
                shutil.copytree(prev["dir"] / "files" / rec["file_key"], out / "files" / key)
                fr = _load_file_json(out / "files" / key)
                fr.sha, fr.file_key, fr.reused_from = e["_sha"], key, prev["job"]
                if rv:
                    rv.carry([d for d in prev["decisions"] if str(d.get("table_ref", "")).startswith(key + ":")])
                progress.log(f"reused files/{key}")
                processed[e["_sha"]] = fr
            else:
                todo.append((e, key, cfg_hash, file_edits))
        processed.update(_process_all(todo, st, rv, scratch, out, progress))
        for sha, fr in processed.items():
            fr.config_hash = cfg_hashes[sha]
            _write_file_json(out, fr)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    progress.stage("finalizing", 0)
    return _finalize(job, st, out, entries, keys, processed, rv, library, llm_pin, progress, edits)


# ------------------------------------------------------------------ findings, inventory, report


def _s09_tables(frs: list[FileResult]) -> list[dict]:
    """Tables of a source in reading order, for the S-09 family check."""
    out = []
    for fr in frs:
        for s in fr.sheets:
            for t in s["tables"]:
                out.append({"ref": t["table_ref"], "sheet_name": s["name"],
                            "columns": [{"name": c["name"], "dominant": c["source_type"]["dominant"]} for c in t["columns"]]})
    return out


def _source_findings(source_id: str, elist: list[dict], frs: list[FileResult], st: Settings):
    """(findings, selection, families) of one source."""
    fs = fnd.Findings(source_id)
    sel = selection.resolve_selection(source_id, elist, frs, st.selection.get(source_id), fs)
    fams = features.families(_s09_tables(frs))
    for fam in fams:
        lines = [f"{d['tables'][0]} -> {d['tables'][1]}: "
                 + ("; ".join(f"{r['from']}~{r['to']} ({r['rule']})" for r in d["renames"]) or "no renames")
                 + (f"; extra {', '.join(d['extra'])}" if d["extra"] else "") for d in fam["drift"]]
        fs.add("S-09", "family_drift", "source", "info",
               "Tables " + ", ".join(fam["members"]) + " look like one dataset split across tabs or files"
               + (": " + " | ".join(lines) if lines else "; their columns agree."),
               scope_id=source_id, suffix=fam["members"][0])
    for elem, fr in zip(elist, frs):
        ffs = fnd.Findings(f"{source_id}/{fr.file_key}")
        fnd.file_findings(ffs, fr.facts)
        for s in fr.sheets:
            fnd.sheet_findings(ffs, s, st.params)
        fs.extend(ffs)
    return fs, sel, fams


def _decision_findings(rv, sources_files: dict) -> list:
    """`assistant_decision` (warn on a selected table, info otherwise or when an Analyst decided) and
    `llm_fallback` (info) - spec 5.4."""
    out = []
    for source_id, keys in sources_files.items():
        for d in (rv.decisions.values() if rv else []):
            if d["outcome"] not in ("changed", "corrected") or d["kind"] == "header_row":
                continue
            ref = str(d["table_ref"])
            file_key, sheet = ref.split(":")[0], ref.split(":")[1]
            if file_key not in keys:
                continue
            fs = fnd.Findings(f"{source_id}/{file_key}")
            sev = "warn" if d["outcome"] == "changed" else "info"
            msg = (f"The local model changed a {d['kind']} decision on {ref} against the rules "
                   f"({d['rules_choice']} -> {d['used']}).")
            if d["kind"] == "footer":
                fs.add("assistant_decision", None, "table", sev, msg, sheet_index=int(sheet),
                       table_key=ref.split(":")[2], table_ref=":".join(ref.split(":")[:3]))
            else:
                fs.add("assistant_decision", None, "sheet", sev, msg, sheet_index=int(sheet),
                       suffix=d["decision_id"].split("@")[0] + ":" + ref.split(":", 2)[-1])
            out.extend(fs)
    if rv and rv.fallback_reason:
        fs = fnd.Findings("job")
        fs.add("llm_fallback", None, "job", "info",
               f"The LLM review stopped early ({rv.fallback_reason}); {rv.left_to_rules} question(s) were decided by "
               f"the rules after {rv.calls} call(s).", scope_id="job")
        out.extend(fs)
    return out


def _finalize(job, st, out, entries, keys, processed, rv, library, llm_pin, progress, edits) -> dict:
    by_source: dict = {}
    for e in entries:
        by_source.setdefault(e["source_id"], []).append(e)
    all_findings, selections, inventory_files, per_source = [], [], [], {}
    for source_id, elist in sorted(by_source.items()):
        frs = [processed[e["_sha"]] for e in elist]
        fs, sel, fams = _source_findings(source_id, elist, frs, st)
        for e, fr in zip(elist, frs):
            inventory_files.append(_inventory_entry(e, fr, source_id, llm_pin))
        selected, resolved = set(sel["tables"]), sel["status"] == "resolved"
        if sel["status"] in ("not_found", "required"):        # errors that leave a source empty are blockers
            for f in fs:
                if f["code"] in fnd.ALWAYS:
                    f["severity"] = "block"
        selections.append(sel)
        per_source[source_id] = (frs, sel, fams, fs, selected, resolved)
    _read_conflicts(entries, keys, st, per_source)
    file_keys = {sid: {fr.file_key for fr in v[0]} for sid, v in per_source.items()}
    extra = _decision_findings(rv, file_keys) + _vanished_files(st, edits, set(keys.values()))
    for sid, (frs, sel, fams, fs, selected, resolved) in per_source.items():
        fs.extend(f for f in extra if f["scope"] not in ("job", "config") and f["id"].split("@")[1].split("/")[0] == sid)
        for f in [x for x in fs if x["code"] == "selection_not_found" and "/" in x["id"]]:
            file_key = f["id"].split("@")[1].split("#")[0].split("/")[1]
            if rv and any(d["outcome"] in ("changed", "corrected") and d["kind"] != "header_row"
                          and str(d["table_ref"]).startswith(file_key + ":") for d in rv.decisions.values()):
                fs.add("assistant_decision", None, "file", "warn",
                       "A selector names a table key that the local model's review retired; select the tables again.",
                       scope_id=f"{sid}/{file_key}", suffix="retired-key")
        if sel["status"] == "resolved":
            sel["layout_groups"] = selection.layout_groups(sel, fams)
            if len(sel["layout_groups"]) > 1:
                diffs = selection.layout_differences(sel["layout_groups"])
                fs.add("multiple_layouts", None, "source", "info",
                       f"Source {sid} has {len(sel['layout_groups'])} layouts; each is mapped on its own. "
                       + "; ".join(f"{d['layout_id']} lacks {d['missing'] or 'nothing'}, adds {d['extra'] or 'nothing'}" for d in diffs)
                       + ".", scope_id=sid)
        else:
            sel["layout_groups"] = []
        for f in fs:
            f["severity"] = fnd.effective_severity(f, selected, resolved)
            f["selected"] = f["scope"] == "table" and f["table_ref"] in selected
            f["source_id"] = sid
            f["action"] = fnd.is_action_item(f, selected)
        all_findings.extend(fs)
    for f in (x for x in extra if x["scope"] in ("job", "config")):
        f.update(selected=False, source_id=None, action=True)    # keeps its own severity (override_orphaned: warn)
        all_findings.append(f)
    for sel in selections:
        sel.pop("names", None)
        for g in sel["layout_groups"]:
            g.pop("names", None)

    _write_llm(out, rv, library, job, processed, st)
    _write_sheet_jsons(out, processed, all_findings, rv, st)
    action_items = sorted((fnd.action_item(f) for f in all_findings if f["action"]), key=lambda a: a["id"])
    inventory = {"schema": "dp.structure.inventory/1", "job_id": job.get("job_id"), "config_version": st.version,
                 "rule_pack": RULE_PACK, "files": sorted(inventory_files, key=lambda x: (x["source_id"], x["relative_path"] or "")),
                 "selection": selections}
    report = _report(st, per_source, processed, all_findings, rv)
    dump_json(out / "inventory.json", inventory)
    dump_json(out / "report.json", report)
    dump_json(out / "action_items.json", action_items)
    progress.stage("writing review workbook", 0)
    export_review(out, inventory, processed, per_source, st, job.get("job_id"), all_findings)
    summary = {"files": len(processed), "sheets": report["extras"]["structure"]["sheets"],
               "tables": report["extras"]["structure"]["tables"], "selected_tables": report["extras"]["structure"]["selected_tables"],
               "rows_out": report["counts"]["rows_out"], "reused_files": report["extras"]["structure"]["reused_files"],
               **{sev: sum(1 for a in action_items if a["severity"] == sev) for sev in ("block", "warn", "info")}}
    return {"status": "succeeded", "output_prefix": str(out), "action_items": action_items,
            "blocking": [a["id"] for a in action_items if a["severity"] == "block"], "summary": summary,
            "artifacts": hash_tree(out)}


def _write_sheet_jsons(out: Path, processed: dict, findings: list, rv, st: Settings) -> None:
    """files/{key}/sheets/NN.json in the `dp.structure.sheet/1` shape of spec §9.4 (value-free)."""
    by_table: dict[str, list] = {}
    for f in findings:
        if f.get("table_ref"):
            by_table.setdefault(f["table_ref"], []).append(f["id"])
    decisions = list(rv.decisions.values()) if rv else []
    for fr in processed.values():
        for s in fr.sheets:
            prefix = f"{fr.file_key}:{s['index']}:"
            mine = sorted((d for d in decisions if str(d["table_ref"]).startswith(prefix)), key=lambda d: d["decision_id"])
            llm = {"mode": st.llm["mode"] if rv else "off", "privacy": st.llm["privacy"], "review": st.llm["review"],
                   "decisions": [d["decision_id"] for d in mine],
                   "overruled": [d["decision_id"] for d in mine if d["outcome"] == "overruled"]}
            dump_json(out / "files" / fr.file_key / "sheets" / f"{s['index']:02d}.json",
                      sheet_json(s, fr.file_key, by_table, llm))


def _vanished_files(st: Settings, edits: list[dict], keys: set) -> list:
    """An override or edit whose file is no longer in the run is not applied and is reported (spec 8.4, 8.5)."""
    fs = fnd.Findings("job")
    for ref in sorted({o["table"] for o in st.overrides if o["table"].split(":")[0] not in keys}
                      | {e["row_id"].rsplit(":", 1)[0] for e in edits if e["row_id"].split(":")[0] not in keys}):
        fs.add("override_orphaned", None, "config", "warn",
               f"An override or cell edit for {ref} was not applied: its file is not part of this run.",
               scope_id=f"config/{ref}")
    for f in fs:
        f["scope"] = "config"
    return list(fs)


def _read_conflicts(entries, keys, st, per_source) -> None:
    """The same content in two sources whose `read` entries differ raises `read_conflict` (spec 6.1)."""
    seen: dict = {}
    for e in entries:
        reads = [json.dumps(r, sort_keys=True) for r in st.read if r.get("file") and _matches(r["file"], e)]
        seen.setdefault(e["_sha"], {})[e["source_id"]] = tuple(reads)
    for sha, per in seen.items():
        if len(per) > 1 and len(set(per.values())) > 1:
            for sid in per:
                fs = per_source[sid][3]
                fs.add("read_conflict", None, "file", "block",
                       "The same file content is in several sources whose read settings differ.",
                       scope_id=f"{sid}/{keys[sha]}")


def _write_llm(out: Path, rv, library: Library, job: dict, processed: dict, st: Settings) -> None:
    """llm/decisions.jsonl and llm/examples.jsonl (spec 10.7, 10.8): value-free, sorted, deterministic."""
    decisions = sorted(rv.decisions.values(), key=lambda d: d["decision_id"]) if rv else []
    (out / "llm").mkdir(exist_ok=True)
    (out / "llm" / "decisions.jsonl").write_text(
        "".join(json.dumps(d, sort_keys=True) + "\n" for d in decisions), encoding="utf8")
    if rv:
        for rec in _corrections(rv, st, processed):
            library.add_correction(rec)
    (out / "llm" / "examples.jsonl").write_text(
        "".join(json.dumps(r, sort_keys=True) + "\n" for r in sorted(library.new, key=lambda r: r["id"])), encoding="utf8")


def _corrections(rv, st: Settings, processed: dict) -> list[dict]:
    """An override that changes a header the review decided becomes a value-free correction (spec 10.7)."""
    out = []
    tables = {t["table_ref"]: t for fr in processed.values() for s in fr.sheets for t in s["tables"]}
    for o in st.overrides:
        t = tables.get(o["table"])
        d = rv.decisions.get(f"header_row@{o['table']}") or rv.prior.get(f"header_row@{o['table']}")
        if not (t and d and o.get("header_rows") is not None):
            continue
        right = f"row:{o['header_rows'][-1]}" if o["header_rows"] else "none"
        if right != d["used"] and right in d["options"]:
            out.append({"kind": "header_row", "question_hash": d["question_hash"], "grid": d["grid"],
                        "options": d["options"], "answer_used": d["used"], "right": right,
                        "structural": [c["label"] for c in t["columns"]]})
    return out


def _inventory_entry(e: dict, fr: FileResult, source_id: str, llm_pin: dict) -> dict:
    entry = {"file_id": e.get("file_id"), "source_id": source_id, "file_key": fr.file_key,
             "relative_path": e.get("relative_path"), "format": fr.format, "converted_from": fr.converted_from,
             "macro_enabled": fr.macro_enabled, "size_bytes": fr.size, "content_hash": "sha256:" + fr.sha,
             "reused_from": fr.reused_from, "status": "blocked" if fr.blocked else "ok",
             "config_hash": fr.config_hash, "rule_pack": RULE_PACK, "pipeline_minor": PIPELINE_MINOR,
             "llm_pin": llm_pin, "sheets": []}
    if fr.parquet is not None:
        entry["tables"] = [{"table_ref": f"{fr.file_key}:0:t1", "data_ref": fr.parquet_path,
                            "rows": fr.parquet["rows"], "columns": len(fr.parquet["columns"])}]
    for s in fr.sheets:
        entry["sheets"].append({"index": s["index"], "name": s["name"], "key": s["key"], "state": s["state"],
                                "used_range": s["used_range"], "populated": s["populated"], "regions": s["regions"],
                                "tables": [t["key"] for t in s["tables"]], "error": s["error"]})
    return entry


def _report(st: Settings, per_source: dict, processed: dict, findings: list, rv) -> dict:
    sel_refs = {r for v in per_source.values() for r in v[1]["tables"]}
    rows_in = rows_out = 0
    per_src, rule_rows, applied = [], {}, set()
    for sid, (frs, sel, *_rest) in sorted(per_source.items()):
        s_in = s_out = 0
        for fr in frs:
            for s in fr.sheets:
                for t in s["tables"]:
                    if t["table_ref"] in sel["tables"]:
                        s_in += t["span_rows"]
                        s_out += t["n_rows"]
            if fr.parquet is not None and f"{fr.file_key}:0:t1" in sel["tables"]:
                s_in += fr.parquet["rows"]
                s_out += fr.parquet["rows"]
        rows_in, rows_out = rows_in + s_in, rows_out + s_out
        per_src.append({"source": sid, "rows_in": s_in, "rows_out": s_out,
                        "reused": all(fr.reused_from for fr in frs)})
    for fr in processed.values():
        for s in fr.sheets:
            for t in s["tables"]:
                inf = t["info"]
                for code, moved in (("S-01", len(inf["preamble_rows"]) + len(t["footer_rows"])), ("S-05", len(t["dropped"])),
                                    ("S-08", len(t["gap_rows"])), ("S-10", inf.get("merge_fills", 0))):
                    if moved:
                        applied.add(code)
                        rule_rows[code] = rule_rows.get(code, 0) + moved
    rules = [{"rule_id": c, "applied": c in applied, "rows_affected": rule_rows.get(c, 0)}
             for c in sorted(applied | {"S-01", "S-05", "S-08"})]
    return {
        "step": "structure", "config_version": st.version, "counts": {"rows_in": rows_in, "rows_out": rows_out},
        "per_source": per_src, "rules": rules,
        "findings": [{"id": f["id"], "severity": f["severity"], "message": f["message"], "series": None}
                     for f in sorted(findings, key=lambda f: f["id"])],
        "extras": {"structure": {
            "pipeline": PIPELINE, "rule_pack": RULE_PACK, "llm": rv.summary() if rv else {"mode": "off", "fallback": False},
            "files": len(processed), "sheets": sum(len(fr.sheets) for fr in processed.values()),
            "tables": sum(len(s["tables"]) for fr in processed.values() for s in fr.sheets),
            "selected_tables": len(sel_refs), "reused_files": sum(1 for fr in processed.values() if fr.reused_from),
            "findings_detail": [{"id": f["id"], "codes": f["codes"], "check": f["check"], "scope": f["scope"],
                                 "table_ref": f["table_ref"], "selected": f.get("selected", False),
                                 "severity": f["severity"], "client_report": f["client_report"],
                                 "locations": f["locations"]} for f in sorted(findings, key=lambda f: f["id"])],
            "review_workbook": "review/Structural_Review.xlsx"}}}
