"""Phase adapters, in pipeline order. Adding a phase = one adapter class + one line here."""
from __future__ import annotations

from .base import Phase
from .hygiene import HygienePhase
from .structure import StructurePhase
from .timeseries import TimeseriesPhase
from .transform import TransformPhase
from .trends import TrendsPhase

ORDER = ["A", "B", "C", "D", "T"]
PHASES: dict[str, Phase] = {p.key: p for p in (StructurePhase(), HygienePhase(), TimeseriesPhase(), TrendsPhase(),
                                                 TransformPhase())}


def previous(key: str) -> str | None:
    i = ORDER.index(key)
    return ORDER[i - 1] if i else None


def following(key: str) -> list[str]:
    return ORDER[ORDER.index(key) + 1:]
