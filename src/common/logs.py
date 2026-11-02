"""The run log all phases share: <run folder>/pipeline.log, plus the console."""
from __future__ import annotations

import logging
from pathlib import Path


def run_logger(run_dir: Path, name: str) -> logging.Logger:
    """Log to <run>/pipeline.log (shared with the other phases) and to the console."""
    run_dir.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger(name)
    if not log.handlers:
        log.setLevel(logging.INFO)
        log.addHandler(logging.StreamHandler())
    for h in [h for h in log.handlers if isinstance(h, logging.FileHandler)]:
        log.removeHandler(h)
        h.close()
    fh = logging.FileHandler(run_dir / "pipeline.log", encoding="utf8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    log.addHandler(fh)
    return log
