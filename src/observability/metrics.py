"""In-process counters in the Prometheus text format (`GET /api/v1/metrics`). No dependency, no labels explosion:
label values are fixed vocabularies (status class, phase key, action, outcome), never ids or user text."""
from __future__ import annotations

import threading
import time
from collections import defaultdict

_lock = threading.Lock()
_counters: dict[tuple[str, tuple[tuple[str, str], ...]], float] = defaultdict(float)
_help: dict[str, str] = {}
STARTED = time.time()


def inc(name: str, help_text: str, value: float = 1.0, **labels: str) -> None:
    with _lock:
        _help.setdefault(name, help_text)
        _counters[(name, tuple(sorted(labels.items())))] += value


def render(gauges: dict[str, tuple[str, float]] | None = None) -> str:
    """Counters, plus `gauges` = {name: (help, value)} read at scrape time."""
    lines: list[str] = []
    with _lock:
        by_name: dict[str, list[tuple[tuple[tuple[str, str], ...], float]]] = defaultdict(list)
        for (name, labels), value in sorted(_counters.items()):
            by_name[name].append((labels, value))
        for name, series in by_name.items():
            lines += [f"# HELP {name} {_help[name]}", f"# TYPE {name} counter"]
            for labels, value in series:
                text = ",".join(f'{k}="{v}"' for k, v in labels)
                lines.append(f"{name}{{{text}}} {value:g}" if text else f"{name} {value:g}")
    for name, (help_text, value) in (gauges or {}).items():
        lines += [f"# HELP {name} {help_text}", f"# TYPE {name} gauge", f"{name} {value:g}"]
    return "\n".join(lines) + "\n"


def reset() -> None:
    """For tests."""
    with _lock:
        _counters.clear()
