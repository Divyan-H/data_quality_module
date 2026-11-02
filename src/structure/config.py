"""Rule pack `structure@1.0.0` and the run's `structure` config section (spec 8.1, 8.2, 8.6)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

RULE_PACK = "structure@1.0.0"
PIPELINE = "dp@1.0.0"

# Scorer lexicons belong to the rule pack: changing them is a new rule-pack version (spec 4.5).
HEADER_LEXICON = {
    "date", "week", "month", "year", "spend", "cost", "impressions", "clicks", "channel",
    "campaign", "region", "geography", "brand", "product", "sales", "revenue", "name", "id",
    "amount", "total", "country", "category", "type", "quantity", "price",
}
FOOTER_LEXICON = (
    "grand total", "subtotal", "sub total", "total", "sum", "average", "avg", "note", "notes",
    "source", "remarks", "remark",
)
UNIT_TOKENS = {
    "inr", "usd", "eur", "gbp", "€", "$", "£", "₹", "%", "units", "unit", "'000", "(000s)", "000s",
    "k", "mn", "m", "cr", "lakhs", "lakh", "bn", "pct",
}

# key -> (default, allowed range or choices)
PARAMS = {
    "row_gap": (2, (2, 10)), "col_gap": (2, (2, 10)), "soft_cuts": (True, bool),
    "max_title_rows": (10, (0, 30)), "max_header_scan_rows": (12, (3, 50)),
    "header_min_score": (4.0, (2.0, 8.0)), "header_tie_margin": (1.0, (0.5, 3.0)),
    "llm_override_margin": (6.0, None),  # bounds depend on header_tie_margin, checked below
    "min_table_cells": (4, (2, 50)), "min_data_rows": (1, (1, 100)),
    "sparse_column_pct": (90, (50, 99)), "sheet_sparsity_pct": (50, (10, 95)),
    "uncached_block_pct": (50, (1, 100)),
    "composite_separator": ("_", ("_", " ", " / ", " - ", ".")),
    "review_max_rows": (100_000, (1_000, 1_000_000)),
}
RULES = {  # (section, key) -> default
    ("S-05", "drop_empty_columns"): True, ("S-08", "enabled"): True,
    ("S-10", "fill_labels"): True, ("S-10", "fill_numeric"): False,
    ("S-22", "use_table_ranges"): True, ("footers", "enabled"): True,
}
LLM = {"mode": ("assist", ("off", "assist")), "privacy": ("full", ("structure_only", "labels_only", "full")),
       "votes": (3, (1, 3, 5)), "review": ("all", ("all", "near_ties"))}


class ConfigInvalid(Exception):
    """Config fails spec 8.2 bounds (`config_invalid`, not retryable)."""


@dataclass
class Settings:
    params: dict                      # flat: params + rule switches (see resolve_settings)
    llm: dict
    read: list = field(default_factory=list)
    selection: dict = field(default_factory=dict)
    overrides: list = field(default_factory=list)
    cell_edits_ref: dict | None = None
    version: int | None = None
    raw_section: dict = field(default_factory=dict)


def _check(name: str, value, spec) -> None:
    if spec is bool:
        ok = isinstance(value, bool)
    elif isinstance(spec, tuple) and len(spec) == 2 and all(isinstance(x, (int, float)) for x in spec) \
            and not isinstance(value, str):
        ok = isinstance(value, (int, float)) and not isinstance(value, bool) and spec[0] <= value <= spec[1]
    else:
        ok = value in spec
    if not ok:
        raise ConfigInvalid(f"structure.{name}={value!r} is outside its allowed values")


def resolve_settings(config: dict | None) -> Settings:
    """Merge the `structure` section over defaults and enforce the bounds of spec 8.2."""
    section = (config or {}).get("structure") or {}
    params = {k: d for k, (d, _) in PARAMS.items()}
    for k, v in (section.get("params") or {}).items():
        if k not in PARAMS:
            raise ConfigInvalid(f"unknown structure.params key {k!r}")
        if k != "llm_override_margin":
            _check(k, v, PARAMS[k][1])
        params[k] = v
    if not params["header_tie_margin"] <= params["llm_override_margin"] <= 12.0:
        raise ConfigInvalid("structure.params.llm_override_margin must lie in [header_tie_margin, 12.0]")
    rules = dict(RULES)
    for sec, kv in (section.get("rules") or {}).items():
        for k, v in kv.items():
            if (sec, k) not in rules or not isinstance(v, bool):
                raise ConfigInvalid(f"bad structure.rules entry {sec}.{k}")
            rules[(sec, k)] = v
    flat = dict(params)
    flat.update(drop_empty_columns=rules[("S-05", "drop_empty_columns")], gap_rows=rules[("S-08", "enabled")],
                fill_labels=rules[("S-10", "fill_labels")], fill_numeric=rules[("S-10", "fill_numeric")],
                use_table_ranges=rules[("S-22", "use_table_ranges")], footers=rules[("footers", "enabled")],
                group_separator=params["composite_separator"])
    llm = {k: d for k, (d, _) in LLM.items()}
    for k, v in (section.get("llm") or {}).items():
        if k not in LLM:
            raise ConfigInvalid(f"unknown structure.llm key {k!r}")
        _check(f"llm.{k}", v, LLM[k][1])
        llm[k] = v
    return Settings(flat, llm, section.get("read") or [], section.get("selection") or {},
                    section.get("overrides") or [], section.get("cell_edits_ref"),
                    (config or {}).get("version"), section)


def file_config_hash(st: Settings, file_key: str, relative_paths: list[str], edit_ids: list[str] | None = None) -> str:
    """Spec 8.6: hash of everything that can change one file's extraction. Selection is excluded."""
    from fnmatch import fnmatch
    payload = {
        "params": st.params, "llm": st.llm,
        "read": [r for r in st.read if any(fnmatch(p, r.get("file", "*")) for p in relative_paths)],
        "overrides": [o for o in st.overrides if o["table"].startswith(file_key + ":")],
        "edits": sorted(edit_ids or []),
    }
    return "sha256:" + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
