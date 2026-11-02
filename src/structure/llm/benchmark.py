"""LLM benchmark (spec 10.9): measures a model against labelled regions, `assist` versus `off`.

Run on the local benchmark machine (pilot files stay there):
    python -m src.structure.llm.benchmark cases.jsonl --model qwen2.5:7b-instruct --digest sha256:...

Each line of `cases.jsonl` is {"file": path, "sheet": name or null, "table": "t1", "header_row": 3,
"footer_rows": [] (optional), "tables": 1 (optional, tables expected in that sheet)}. Labels hold no values.
"""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from ..pipeline import run_structure
from .review import LLMConfig

TARGETS = {"header": 0.98, "boundaries": 0.97, "footers": 0.95, "parse_rate": 0.995, "p95_seconds": 15.0}


def _facts(out: Path, case: dict) -> dict | None:
    facts = json.loads(next(out.glob("files/*/file.json")).read_text(encoding="utf8"))
    for s in facts["sheets"]:
        if case.get("sheet") in (None, s["name"]):
            return next(({"sheet": s, "table": t} for t in s["tables"] if t["key"] == case["table"]), None)
    return None


def run_case(case: dict, llm: dict | None, pin: dict, transport=None) -> tuple[dict, dict | None]:
    """Run one labelled file. Returns (facts of its labelled table, LLM summary)."""
    config = {"structure": {"llm": {"mode": "assist", **llm}}} if llm is not None else {}
    with tempfile.TemporaryDirectory() as tmp:
        run_structure({"files": [{"path": case["file"], "source_id": "s-1", "file_id": "f", "relative_path": Path(case["file"]).name}],
                                "config": config, "output_dir": str(Path(tmp) / "out"), "llm": pin, "llm_transport": transport})
        found = _facts(Path(tmp) / "out", case)
        report = json.loads((Path(tmp) / "out" / "report.json").read_text(encoding="utf8"))
    return found, report["extras"]["structure"]["llm"] if llm is not None else None


def score(cases: list[dict], llm: dict | None, pin: dict, transport=None) -> dict:
    header = boundaries = footers = n_foot = calls = parsed = 0
    slowest = 0.0
    for case in cases:
        found, summary = run_case(case, llm, pin, transport)
        if found is not None:
            t = found["table"]
            header += t["header_rows"][-1:] == [case["header_row"]]
            if "footer_rows" in case:
                n_foot += 1
                footers += t["footer_rows"] == case["footer_rows"]
        boundaries += found is not None and case.get("tables", len(found["sheet"]["tables"])) == len(found["sheet"]["tables"])
        if summary:
            calls, parsed = calls + summary["calls"], parsed + summary["parsed"]
            slowest = max(slowest, summary["latency_p95_s"])
    n = max(len(cases), 1)
    return {"header": header / n, "boundaries": boundaries / n, "footers": footers / n_foot if n_foot else None,
            "parse_rate": parsed / calls if calls else None, "p95_seconds": slowest if calls else None}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Benchmark a local model for the Structure review")
    ap.add_argument("cases")
    ap.add_argument("--model", required=True)
    ap.add_argument("--digest")
    ap.add_argument("--privacy", default="full", choices=["structure_only", "labels_only", "full"])
    a = ap.parse_args(argv)
    cases = [json.loads(x) for x in Path(a.cases).read_text(encoding="utf8").splitlines() if x.strip()]
    pin = {"model": a.model, "model_digest": a.digest, "prompt_version": LLMConfig.prompt_version}
    report = {"off": score(cases, None, pin), "assist": score(cases, {"privacy": a.privacy}, pin), "targets": TARGETS}
    ok = all((report["assist"][k] or 0) >= report["off"][k] for k in ("header", "boundaries"))   # never below `off`
    report["assist_at_least_as_good_as_off"] = ok
    print(json.dumps(report, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
