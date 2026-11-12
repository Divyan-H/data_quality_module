"""Worker process: runs ONE phase action for ONE run, then exits.

    python -m src.orchestration.worker <job_dir>

`<job_dir>/request.json` says what to do; the outcome goes to `<job_dir>/result.json`. Live progress and log lines
go to the run's `events.jsonl`. Running a phase in its own process gives the service: cancellation (kill the process
tree), isolation (a crash or a memory blow-up cannot take the API down), and a clean logging state per job (the phases
configure global loggers).
"""
from __future__ import annotations

import json
import logging
import sys
import time
import traceback
import warnings
from pathlib import Path

from src.orchestration.phases import PHASES
from src.orchestration.phases.base import Context, Emitter, EventLogHandler, PhaseError
from src.storage.audit import AuditLog


def _instrument_llm(emit: Emitter, audit: AuditLog, run_id: str | None, phase: str, purpose: str) -> dict:
    """Show LLM activity live and record every call to the local model in the audit database: model, build digest,
    timing, sizes and whether an answer came back. The prompt and the answer themselves are never stored."""
    from src.structure.llm.client import OllamaClient
    original = OllamaClient.chat
    state = {"calls": 0, "failed": 0, "seconds": 0.0}

    def chat(self, prompt, schema, seed, temperature, **kw):
        t0 = time.monotonic()
        out = original(self, prompt, schema, seed, temperature, **kw)
        took = time.monotonic() - t0
        state["calls"] += 1
        state["failed"] += out is None
        state["seconds"] += took
        audit.llm_call(purpose=purpose, model=self.model, digest=self.digest, latency_ms=int(took * 1000),
                       outcome="ok" if out is not None else "no_answer", run_id=run_id, phase=phase,
                       prompt_version=kw.get("prompt_version"), prompt_chars=len(prompt) + len(kw.get("system") or ""),
                       answer_chars=len(json.dumps(out)) if out is not None else 0, attempt=state["calls"])
        emit.stage("LLM review", None, f"{state['calls']} call(s) to {self.model}, {state['seconds']:.0f}s so far"
                   + (f", {state['failed']} unanswered" if state["failed"] else ""))
        return out

    OllamaClient.chat = chat          # type: ignore[method-assign,assignment]  # instrumentation of this one process
    return state


def main(job_dir: str) -> int:
    warnings.filterwarnings("ignore")
    job = Path(job_dir)
    req = json.loads((job / "request.json").read_text(encoding="utf8"))
    run = Path(req["run_dir"])
    emit = Emitter(run, req["job_id"], req["phase"])
    handler = EventLogHandler(emit)
    for name in ("dq.hygiene", "dq.timeseries", "dq.trends", "dq.agent"):
        lg = logging.getLogger(name)
        lg.setLevel(logging.INFO)
        lg.addHandler(handler)               # also keeps the phases from adding their own console handler
    phase = PHASES[req["phase"]]
    action = req["action"]
    audit = AuditLog(req.get("audit_db"))
    llm_state = _instrument_llm(emit, audit, (req.get("state") or {}).get("id"), phase.key,
                                "agent.plan" if action.startswith("agent_") else "structure.review")
    ctx = Context(run=run, emit=emit, settings_path=req["settings_path"], state=req.get("state", {}))
    params = req.get("params", {})
    result: dict
    try:
        emit.stage("starting", 0)
        if action == "run":
            data = phase.run(ctx, params)
        elif action == "review":
            data = phase.review(ctx, Path(req["upload"]), params)
        elif action == "candidates":
            data = {"tables": phase.candidates(ctx)}
        elif action == "columns":
            data = {"tables": phase.columns(ctx)}
        elif action == "template":
            data = {"path": str(phase.make_template(ctx, params.get("table")))}
        elif action == "check_config":
            data = phase.check_config(ctx, Path(req["upload"]), params.get("table"))
        elif action.startswith("agent_"):
            from src.orchestration.agent import run_action
            emit.stage({"agent_message": "planning", "agent_apply": "applying the change",
                        "agent_undo": "undoing the last change"}.get(action, action), 10)
            data = run_action(phase, ctx, action, params)
        else:
            raise PhaseError("bad_action", f"unknown action {action}")
        if action in ("run", "review", "agent_apply", "agent_undo"):
            emit.stage("summarising", 100)
            (run / "summaries").mkdir(exist_ok=True)
            (run / "summaries" / f"{phase.key}.json").write_text(
                json.dumps(phase.summary(run), ensure_ascii=False, default=str), encoding="utf8")
        if action in ("run", "review"):                 # the summary lives in summaries/<phase>.json; keep only what changed
            data = {"applied": data.get("applied")} if data.get("applied") else {}
        result = {"ok": True, "data": data}
    except PhaseError as e:
        emit.log(e.message, "error")
        result = {"ok": False, "kind": "rejected" if e.code == "upload_rejected" else "error", "code": e.code,
                  "message": e.message, "problems": e.problems}
    except Exception as e:  # noqa: BLE001 - last line of defence: report, never hang the run
        (job / "worker.log").write_text(traceback.format_exc(), encoding="utf8")
        emit.log(f"{type(e).__name__}: {str(e)[:300]}", "error")
        result = {"ok": False, "kind": "crash", "code": "internal_error",
                  "message": f"{type(e).__name__}: {str(e)[:300]}", "problems": []}
    result["llm_calls"] = llm_state["calls"]
    (job / "result.json").write_text(json.dumps(result, ensure_ascii=False, default=str), encoding="utf8")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
