# Spec DP-G — Pre-Load Database Checks

> **Status:** Proposed · **Version:** 0.3 · **Series:** Data Pipeline phase spec G of 7 (A–G)
>
> **This is a new microservice, designed fresh.** **Architecture: output is written as Parquet, then loaded into DuckDB** — not MySQL.
>
> **Where this phase sits.** An upstream mapping phase produces one mapped/harmonized Parquet file per series (containing only the columns it chose to keep for the database — a discarded column was never written to this Parquet at all). This phase validates that Parquet. A downstream loading phase then reads this phase's `load_gate_report.json` (§4) and refuses to write to DuckDB unless it says `load_permitted: true`.

---

# 1. Scope

| Code | Description | Applies to |
|---|---|---|
| E-11 | Schema validation before loading | Parquet schema vs. DuckDB target table schema |
| E-16 | Reporting granularity / time-period splitting | Series grain conformance, checked before load |
| E-10 | Large DB dump / independent DB isolation | DuckDB file targeting (dev/test vs. production) and load-size sanity |

All three run **after** the mapping phase produces the mapped, filtered Parquet, and **before** the loading phase writes it into DuckDB. This phase is the gate between "Parquet is ready" and "Parquet is loaded."

---

# 2. Grain Model

Each series declares its own grain — the dimension columns that together make a row unique. Default set (config, not hardcoded — a client's plugin can add series or change grains):

| Series | Declared grain |
|---|---|
| Spend | Date, Geography, Channel, Campaign |
| Sales | Date, Geography, Brand, Product |
| Volume | Date, Geography, Brand, Product |
| Impressions | Date, Geography, Channel, Campaign, Platform |
| Conversions | Date, Geography, Channel, Platform |

This table is the reference point both E-16 (does the data match its series' grain) and E-11 (does the Parquet schema have every column the grain and the DuckDB table require) check against.

---

# 3. Rule Catalogue

| Code | Rule | Class | Detection | Remediation | Config |
|---|---|---|---|---|---|
| E-11 | Parquet schema doesn't match the DuckDB target table's declared schema | Block | Compare Parquet column names + types against the target table DDL (from the grain/schema config, §2); any missing column, type mismatch, or unexpected extra column fails | None — load is rejected; mismatch is reported for the Analyst/engineer to fix upstream (mapping or schema config) | `allow_extra_columns` (bool, default false) |
| E-16 | Rows don't conform to their series' declared grain | Block (default) / Warn | Group Parquet rows by their series' grain columns; any duplicate grain-key combination, or any grain column with nulls, fails | None — load is rejected for that series; rows are not silently collapsed or dropped | `severity` per series (block/warn), default block |
| E-10a | Load target resolves to the production DuckDB file without an explicit production flag on the job | Block | Compare the load job's target DB path against the configured production path; job must carry `target: production` to proceed | None — job is rejected, must be resubmitted with the flag | `production_db_path` |
| E-10b | Load would write an unexpectedly large row count in one job | Warn | Row count to be written exceeds N× the target table's current row count (default N=3) or a flat row-count ceiling, whichever config sets | None — proceeds with a logged warning, not a block, since a legitimately large backfill is a normal first load | `size_multiplier` (default 3), `absolute_ceiling` |

E-10 splits into two checks (a) and (b) because the old code's single description ("large DB dump / independent DB isolation") conflates two different failure modes — writing to the wrong database, and writing an unexpectedly large amount to the right one. Keeping them separate lets each have its own severity: (a) is always a hard stop (there's no legitimate reason to hit production without the flag), (b) is a sanity warning (a large load can be entirely correct, e.g. a client's first backfill).

---

# 4. Output

A `load_gate_report.json`, written before the downstream loading phase runs:

```json
{
  "series": "spend",
  "checks": {
    "E-11_schema_validation": {"status": "pass"},
    "E-16_grain_conformance": {"status": "fail", "duplicate_grain_keys": 3, "examples": ["2026-01-06|US|Facebook|null"]},
    "E-10a_db_isolation": {"status": "pass", "target": "staging"},
    "E-10b_load_size": {"status": "warn", "row_count": 480000, "baseline": 120000, "ratio": 4.0}
  },
  "load_permitted": false
}
```

`load_permitted` is `false` if any Block-class check fails, regardless of Warn-class results. The loading phase downstream reads this report and refuses to write to DuckDB for that series if `load_permitted` is `false`.

---

# 5. Configuration

One config block per series (grain, schema) plus one service-wide block (production path, size thresholds):

```json
{
  "grain_config": {
    "spend": {"dimensions": ["date", "geography", "channel", "campaign"], "e16_severity": "block"},
    "sales": {"dimensions": ["date", "geography", "brand", "product"], "e16_severity": "block"}
  },
  "load_safety": {
    "production_db_path": "/data/prod/ads.duckdb",
    "size_multiplier": 3,
    "absolute_ceiling": 5000000,
    "allow_extra_columns": false
  }
}
```

---

# 6. Analyst Review

Per-series failures in `load_gate_report.json` surface as items on a review screen (not a separate download/upload workbook — this phase's failures are structural/config problems, not row-level data decisions, so they route back to whoever owns the mapping/grain config rather than to an Analyst editing values). E-10b warnings are acknowledge-and-proceed, matching the block/warn pattern used elsewhere in this spec series.

---

# 7. Open Questions

1. Does a series-level E-11/E-16 block stop only that series' load, or the whole run's load (all series in one Parquet batch)? This spec assumes per-series isolation — recommend deciding this once and applying it consistently to the downstream loading phase too.
2. Schema evolution: if a new column legitimately needs to reach the DuckDB table (a new dimension added later), what's the process to update the target schema itself, vs. this phase correctly rejecting it as a mismatch?
3. `size_multiplier`'s baseline — "current row count" — is ambiguous for a series' very first load (no baseline exists yet). Recommend: skip E-10b entirely when the target table doesn't exist yet or is empty.
4. Who resolves an E-11/E-16 failure in practice — does it route back to the mapping phase's review screen automatically, or is it a manual fix outside this spec's flow?

---
