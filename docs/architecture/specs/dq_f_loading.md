# Spec DP-F — Loading Parquet into DuckDB

> **Status:** Proposed · **Version:** 0.3 · **Series:** Data Pipeline phase spec F of 7 (A–G)
>
> **This is a new microservice, designed fresh.** **Architecture: Parquet → DuckDB.**
>
> **What this phase receives.** The mapping phase produces one Parquet file per series, containing only the columns it marked `Include` (a `Drop`ped column was never written to this Parquet at all). Before this phase runs, a separate pre-load validation step checks that Parquet against three things — the target DuckDB table's schema, each series' grain (the dimension columns that together make one row unique, listed in §5), and DB-isolation/size sanity — and writes one `load_gate_report.json` per series, in the exact shape shown in §4 step 1. **This phase only proceeds when that report says `load_permitted: true`.**

---

# 1. Scope

| Code | Description | Design resolution |
|---|---|---|
| E-07 | Source-to-fact row alignment | Resolved by design (§2) — full-refresh load has no "existing" row to misalign with |
| E-08 | NULL values in existing fact columns | Reframed as a post-load sanity check (§3) |
| E-09 | Duplicate-row risk during incremental loading | Resolved by design (§2) — no incremental append, so no accumulation risk |
| E-15 | Loading without unnecessary deduplication | Directly answered by §2's dedup-once rule |

---

# 2. Load Model: Full-Refresh, Not Incremental

**Decision:** each successful run replaces the DuckDB table's contents wholesale for the series it loads — the table's contents are always exactly what the current run's Parquet output says, nothing older layered underneath.

Why this over incremental append: the upstream mapped, validated Parquet already represents the full current state of a series, not a delta. Appending to a growing fact table would require solving exactly the problems E-07/E-08/E-09 exist to catch — row alignment against prior loads, stale-NULL handling, duplicate accumulation — for no benefit, since the source is already a full snapshot each time. Full-refresh sidesteps all three by construction:

- **E-07 (row alignment):** there's no "existing" fact row to align against — the table is truncated and rebuilt from the current Parquet each run.
- **E-09 (duplicate-row risk):** nothing accumulates across runs, so there's no cross-run duplicate to risk.
- **E-15 (unnecessary dedup on load):** deduplication happens exactly once, earlier in the pipeline (at the cell-hygiene/cleaning stage, which drops exact duplicate rows before this phase ever sees the data) — this load step must not dedup again. If a duplicate grain-key still reaches this step, that means the upstream dedup or the pre-load grain check (§1's `load_gate_report.json`) missed it — a bug to surface loudly, not a load-time cleanup job to paper over it.

**Trade-off, noted for the record:** full-refresh means a load failure partway through needs a transaction boundary (§4) so the table is never left half-replaced. It also means history isn't kept inside DuckDB itself — if "what did this table look like last week" matters, that lives in the versioned `ads@vN` Parquet, not in DuckDB, which only ever holds the latest load.

---

# 3. Rule Catalogue

| Code | Rule | Class | Detection | Remediation |
|---|---|---|---|---|
| E-07 | *(structural guarantee, not a runtime rule)* | — | N/A under full-refresh | — |
| E-08 | A loaded column is unexpectedly all-NULL | Warn | Post-load: any column present in the target schema has 100% NULL values in the freshly-loaded table | None — flags for investigation; likely a broken source feed or a mapping gap upstream, not something to auto-fix here |
| E-09 | *(structural guarantee, not a runtime rule)* | — | N/A under full-refresh | — |
| E-15 | Silent dedup during load | Block | Any grain-key duplicate found at load time is treated as a hard failure, not silently collapsed | Load aborts; this is a final guard inside the load transaction, restating the same grain-key uniqueness check the pre-load step already ran (§1) |

---

# 4. Processing

1. Read that series' `load_gate_report.json`, produced by the pre-load validation step, in this shape:
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
   `load_permitted` is `false` whenever any check above has `status: "fail"` (a `"warn"` alone doesn't block it). **Abort this phase for that series if `load_permitted` is not `true`.**
2. Open a DuckDB transaction.
3. `CREATE OR REPLACE TABLE <series>_staging AS SELECT * FROM read_parquet('<path>')` — load into a staging table first, not directly into the live table.
4. Run E-15's grain-key uniqueness check and E-08's all-NULL column check against `<series>_staging`.
5. If both pass: swap `<series>_staging` into the live `<series>` table (rename, within the same transaction) and commit.
6. If either fails: roll back — the live table is untouched, and nothing partially loads.

This staging-then-swap pattern is what gives the "never left half-replaced" guarantee from §2's trade-off note — a mid-load failure never leaves the live table in a mixed state.

---

# 5. Column & Row Scope

- **Columns:** only attributes the mapping phase marked `Include` reach this step at all (see header note) — a `Drop`ped column was never written to the Parquet this step reads from.
- **Series grain**, for reference (the dimension columns a series' rows must all carry, checked by the pre-load step, §4 step 1):

| Series | Grain |
|---|---|
| Spend | Date, Geography, Channel, Campaign |
| Sales | Date, Geography, Brand, Product |
| Volume | Date, Geography, Brand, Product |
| Impressions | Date, Geography, Channel, Campaign, Platform |
| Conversions | Date, Geography, Channel, Platform |

- **Rows:** row-level inclusion (excluding specific rows or an entire source from a given load, distinct from the column-level include/drop decision made at mapping time) is out of scope for this spec version — noted as an open question (§6) rather than designed here, since it wasn't part of the original ask.

---

# 6. Open Questions

1. Row-level include/exclude at load time (an "Include Y/N per row" control) — does this service need it, or does column-level include/drop plus upstream row-level fixes (during cleaning/harmonizing) cover every real case?
2. Should `<series>_staging` failures (E-08/E-15) be visible anywhere before the next run, or only in that run's job log?
3. Multi-series loads: does one series' failure block only that series' swap, or hold up every series in the same run? Recommend deciding this once and applying it consistently to both this phase and the pre-load validation step.
4. Retention: since DuckDB only ever holds the latest load (§2), is there a need for this service to also expose a "load a specific prior `ads@vN` version into DuckDB" operation (e.g. for debugging), or is that entirely out of scope for V1?

---
