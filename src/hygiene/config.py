"""Phase B settings: built-in defaults, overridden by the `hygiene:` section of config/settings.yaml."""
from __future__ import annotations

from pathlib import Path

import yaml

# Rule id -> does the pipeline change values itself (True) or only send the issue to review (False)?
# A fix switched off here is still detected: the change it would make is proposed in Needs_Review instead.
# D-27 (isolated zero / blank filled from its neighbours) is opt-in everywhere: off, it does nothing, because a filled zero is
# no longer seen by Phases C and D. The web app starts it off (service setting `hygiene_defaults_off`) and offers it on its start screen.
# D-05, D-06 and D-23 have no fix: a missing formula result, an external workbook or cut-off text cannot be rebuilt.
# D-15 is flag-only (a numeric column that has negative values): whether a negative is valid is the data provider's to confirm.
FIXES = {"D-01": True, "D-02": True, "D-03": True, "D-04": True, "D-07": True, "D-08": True,
         "D-09": True, "D-10": True, "D-11": True, "D-12": True, "D-13": True, "D-14": True,
         "D-16": True, "D-18": True, "D-19": True, "D-20": True, "D-21": True, "D-22": True, "D-24": True,
         "D-25": True, "D-27": False, "E-01": True, "N-02": True}

# What each rule does, shown on the Summary sheet and next to every fix in the review workbook.
RULE_TEXT = {
    "D-01": "Number shown as a date -> its Excel serial number",
    "D-02": "Impossible date clamped (Feb 30 -> Feb 28); invalid month/year -> NULL + review",
    "D-03": "Dates rewritten in the column's most common format",
    "D-04": "Mixed types: text in a numeric column -> NULL + review",
    "D-05": "Formula with no stored result (value is NULL) - review",
    "D-06": "Formula refers to another workbook - review",
    "D-07": "#REF! -> NULL",
    "D-08": "Excel error value (#N/A, #VALUE! ...) -> NULL",
    "D-09": "Leading/trailing/double spaces removed",
    "D-10": "Case variants -> the column's most common spelling",
    "D-11": "Spaces removed from column name",
    "D-12": "Near-duplicate names merged; placeholder/sentinel values -> NULL",
    "D-13": "Control / non-printable characters -> space",
    "D-14": "Outlier capped to the nearest normal value",
    "D-15": "Numeric column with negative values - flagged, nothing changed - client",
    "D-16": "Column renamed from the data dictionary; ambiguous names -> client",
    "D-18": "Null placeholder (NA, N/A, null, -) -> NULL",
    "D-19": "Exact duplicate row dropped; same key with different values -> review",
    "D-20": "Garbled text (mojibake) re-decoded",
    "D-21": "Column kept as text (too many digits for a number / identifier)",
    "D-22": "Mixed units converted to the column's majority unit",
    "D-23": "Values cut at the same length (possible truncation) - client",
    "D-24": "Leading zeros restored",
    "D-25": "Excel date serial -> date",
    "D-27": "Isolated zero or blank value in a dated series filled with the mean of the period before and after",
    "D-30": "Same city written differently (another name, short form or spelling mistake) - flagged with the standard name, nothing changed - client",
    "D-31": "Dates not in order within a series (or sorted newest first) - flagged, nothing changed",
    "D-32": "A column the project expects is not in the sheet - flagged, nothing changed - client",
    "D-33": "Amounts carry a currency symbol and no currency code - read in the project's default currency (rupees); another symbol is flagged",
    "D-29": "More than one value in a cell (a list, or a comma that could be a decimal, a thousands mark or two numbers) - flagged, nothing changed - client",
    "D-28": "Column that is the sum of other columns (e.g. Total = Product A + B + C): a derived figure, not a fact - flagged, nothing changed",
    "E-01": "Currency symbols and thousands separators removed",
    "N-02": "Blank text -> NULL",
    "analyst": "Analyst edit from the review workbook",
    "agent": "Change made through the Fix-with-agent chat, approved by the analyst",
}

# Checklist ids that share one implementation with a D-code (audit and report show both).
ALIASES = {"D-04": ["N-01"], "D-18": ["N-03"], "D-09": ["N-04"], "D-13": ["N-05"], "N-02": [], "E-01": ["N-01"]}

DEFAULTS = {
    "fixes": FIXES,
    "currency_symbols": "$€£₹¥",
    "null_tokens": ["na", "n/a", "n.a.", "null", "none", "nil", "nan", "-", "--", "."],
    "dominant_value_threshold": 0.05,  # a token that is the column's top value at >= 5% is a real value
    "suspect_tokens": ["test", "dummy", "sample", "placeholder", "xxx", "tbd", "todo", "unknown", "temp", "fake", "mock"],
    "dayfirst": True,                  # reading of a/b/yyyy when the column gives no evidence
    "min_year": 1900, "max_year": 2100,
    "serial_range": [18264, 60000],    # Excel date serials accepted by D-25: 1950-01-01 .. 2064; smaller whole numbers are counters (week 1..52), not dates
    "id_keywords": ["id", "code", "key", "ref", "identifier", "zip", "postal", "pin", "sku",
                    "phone", "mobile", "account", "card"],
    "zero_pad_keywords": ["id", "code", "key", "ref", "zip", "postal", "pin", "sku"],   # D-24 pads only these
    "fixed_width_keywords": ["zip", "postal", "pin", "pincode"],   # padded even with no leading zero in the column
    "numeric_keywords": ["spend", "cost", "impressions", "clicks", "views", "reach", "sessions", "conversions",
                         "revenue", "count", "rate", "ctr", "cpm", "cpc", "roas", "budget", "amount", "value",
                         "total", "price", "quantity", "qty", "fee", "balance", "weight", "score", "percent", "pct"],
    "date_keywords": ["date", "week", "month", "period", "day", "time"],
    "numeric_min_share": 0.8,          # a column is numeric when >= 80% of its values are numbers (D-04)
    "money_keywords": ["spend", "cost", "amount", "price", "revenue", "budget", "fee", "balance", "sales", "value"],
    "outlier_z": 3.5,                  # robust (median/MAD) z-score
    "outlier_max_share": 0.05,         # cap outliers only when they are at most 5% of the column
    "magnitude_factor": 10,            # value >= 10x or <= 1/10 of the column median
    "min_outlier_values": 8,
    "unit_ratio": 50, "median_multiplier": 10,
    "truncation": {"min_length": 20, "at_max_fraction": 0.15, "at_max_count": 3},
    "max_categories": 200,             # D-10 / D-12 only look at columns with this few distinct values
    "near_dup_ratio": 0.9,
    "max_significant_digits": 15,
    "data_dictionary": None,           # optional csv/xlsx: column name [, standard name] (D-16 renames to the 2nd)
    "zero_expected": {},               # D-27: {table ref: [columns where zero is a real value]}; the analyst ticks them in the UI
    "zero_heavy_share": 0.5,           # D-27: a column with at least this share of zeros is not treated as having missing values
    "max_blank_share": 0.3,            # D-27: a column blank in more than this share of rows is optional, not missing
    "multi_value_separators": [[";", ";"], ["|", r"\|"], ["/", "/"], ["&", "&"], [", ", r",\s+"]],   # D-29: [label, regex] a text cell is split on
    "multi_value_min_part": 2,         # D-29: every part of a list needs at least this many characters (keeps R&D, AT&T, N/A out)
    "multi_value_max_share": 0.5,       # D-29: more cells than this share with lists = the column is a list field: one flag, not one per cell
    "multi_value_max_cells": 200,       # D-29: most cells listed one by one per column (the total is reported)
    "multi_value_convention_cells": 3,  # D-29: a comma pattern used by this many cells is the column's own convention, not ambiguous
    "city_columns": ["city", "cities", "town"],   # D-30: a column whose name has one of these words holds city names
    "city_names_file": "config/city_names.yaml",   # D-30: editable list of cities with their other names, short forms and misspellings
    "city_known_file": "config/india_cities.txt",  # D-30: fallback list of known cities (a value found only here is a low-priority note)
    "city_master": [],                  # D-30: the project's own city list, when it has one (these spellings win; others are flagged)
    "city_gazetteer": None,             # D-30: optional csv / tsv with name, canonical, country for wider coverage
    "city_countries": [],               # D-30: only gazetteer rows of these country codes are loaded (empty = all)
    "city_max_edit": 2,                 # D-30: most edits a longer name may be from a known one (a name up to 5 letters: 1)
    "city_max_items": 300,              # D-30: most distinct spellings listed per column (the total is reported)
    "default_currency": "INR",          # D-33: what an amount with only a symbol is read as; the run inputs' currency.default sets it
    "currency_symbol_codes": {"₹": "INR", "$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY"},
    "column_synonyms": {                # D-32: other ways a header can be written for an expected column (ignoring case, spaces, punctuation)
        "date": ["day", "week", "week start", "week starting", "week commencing", "week start date", "week ending", "month", "period",
                 "report date", "order date", "transaction date"],
        "platform": ["channel", "media", "media channel", "source", "network"],
        "subplatform": ["sub platform", "sub-platform", "sub_platform", "subchannel", "sub channel", "placement", "publisher", "sub source"],
        "city": ["cities", "town", "city name", "location", "market"],
    },
    "expected_columns": {},             # D-32: {"all_sheets": [...], "sheets": {name: [...]}, "strict": false} from the run inputs
    "columns_to_check": [],             # run inputs: [{"table": sheet / file / "*", "columns": [...] or None}]; empty = every column
    "kpi_buckets": True,               # D-28: flag a column that is the sum of other columns (flags only, nothing is changed)
}


def load_config(path: str | None = None, overrides: dict | None = None) -> dict:
    """Defaults < `hygiene:` section of the YAML file < explicit overrides. `fixes` is merged key by key."""
    cfg = {**DEFAULTS, "fixes": dict(FIXES)}
    for src in ((yaml.safe_load(Path(path).read_text(encoding="utf8")) or {}).get("hygiene", {}) if path else {},
                overrides or {}):
        for k, v in src.items():
            if k not in DEFAULTS:
                raise ValueError(f"unknown hygiene setting: {k}")
            cfg[k] = {**cfg[k], **v} if k == "fixes" else v
    return cfg
