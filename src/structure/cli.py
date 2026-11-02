"""Run Structure locally:  python -m src.structure.cli <files-or-folders...> -o out [-c config.json]"""
from __future__ import annotations

import argparse
import json
import os
import sys
from contextlib import nullcontext

from .pipeline import JobFailed, run_structure

SKIP = {".ds_store", "thumbs.db"}


def collect(paths: list[str], source_id: str) -> list[dict]:
    files = []
    for p in paths:
        if os.path.isdir(p):
            for root, _dirs, names in os.walk(p):
                for n in sorted(names):
                    if n.lower() in SKIP or n.startswith("~$"):
                        continue
                    full = os.path.join(root, n)
                    files.append({"path": full, "source_id": source_id,
                                  "relative_path": os.path.relpath(full, p).replace(os.sep, "/")})
        else:
            files.append({"path": p, "source_id": source_id, "relative_path": os.path.basename(p)})
    for i, f in enumerate(files, start=1):
        f["file_id"] = f"f-{i}"
    return files


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Phase A: Structure")
    ap.add_argument("inputs", nargs="+")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("-c", "--config", help="run config JSON (structure section)")
    ap.add_argument("-s", "--source", default="s-1")
    ap.add_argument("--llm-model", help="pin a local Ollama model and use the LLM review (default: rules only)")
    ap.add_argument("--llm-digest", help="model digest to pin, e.g. sha256:...")
    a = ap.parse_args(argv)
    with open(a.config, encoding="utf8") if a.config else nullcontext() as f:
        config = json.load(f) if f else {}
    job = {"files": collect(a.inputs, a.source), "config": config, "output_dir": a.output}
    if a.llm_model:
        job["llm"] = {"model": a.llm_model, "model_digest": a.llm_digest}
    else:                                      # no model pinned: rules only, no fallback finding
        config.setdefault("structure", {}).setdefault("llm", {})["mode"] = "off"
    try:
        res = run_structure(job)
    except JobFailed as e:
        print(f"FAILED {e}", file=sys.stderr)
        return 2
    for item in res["action_items"]:
        print(f"[{item['severity']:5}] {item['message']}")
    print(f"done: {len(res['action_items'])} action item(s), {len(res['blocking'])} blocking -> {a.output}")
    return 1 if res["blocking"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
