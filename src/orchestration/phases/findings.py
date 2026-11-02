"""Reading Phase C/D `findings.json` without loading it: a JSON list with one finding per line, up to hundreds of MB."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


def stream(path: Path):
    """Yield each finding of a `findings.json` (one JSON object per line between `[` and `]`)."""
    try:
        with Path(path).open(encoding="utf8") as fh:
            for line in fh:
                line = line.strip().rstrip(",")
                if line and line not in ("[", "]", "[]"):
                    try:
                        item = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(item, dict):
                        yield item
    except OSError:
        return


def preview(path: Path, per_code: int = 40, total: int = 400, only_status: str | None = "open") -> dict:
    """{counts per code, rows}: the first `per_code` findings of each code (so one noisy code cannot hide the others),
    at most `total` rows overall."""
    counts: Counter = Counter()
    kept: Counter = Counter()
    rows = []
    n = 0
    for f in stream(path):
        n += 1
        if only_status and f.get("status") != only_status:
            continue
        code = f["code"]
        counts[code] += 1
        if kept[code] < per_code and len(rows) < total:
            kept[code] += 1
            rows.append({"id": f["finding_id"], "code": code, "check": f.get("check"), "status": f.get("status"),
                         "variables": f.get("variables", [])[:6], "periods": f.get("periods", [])[:4],
                         "reason": f.get("reason", ""), "client_report": f.get("client_report", False)})
    return {"counts": dict(sorted(counts.items())), "rows": rows, "total": n}
