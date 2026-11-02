# Product Requirement Document — Data Pipeline Service (Data Quality / ETL Module)

## Executive Summary
The Data Pipeline Service is a new microservice that takes raw client Excel/CSV files through structural cleanup, cell-level hygiene, time-series and trend validation, source-to-target mapping, and database loading — ending with validated data in DuckDB. It consolidates and formalizes checks previously spread across the `data_quality_agent` repo and three standalone scripts (`combined_report_app.py`, `Spend_Support_Trend_Report.py`, `Data_Validation_Report_fixed.py`) into seven sequential phases (A–G), each with its own spec, each independently buildable. It is new work — the old repo and scripts are reference material, not a system being ported. Architecture: output is written as Parquet and loaded into DuckDB (full-refresh, not incremental); every phase runs with an Analyst download → edit → upload review loop before its output is accepted downstream.

## Problem Statement
Today, data-quality checks for client Excel/CSV files exist as disconnected pieces: the `data_quality_agent` repo covers structural detection and cell-level remediation; `Data_Validation_Report_fixed.py` and `Spend_Support_Trend_Report.py` separately implement execution/time-series and trend checks with no shared contract between them; and there is no formal, auditable path from a cleaned file into a database. Issues get caught (or missed) inconsistently depending on which script a file happens to pass through, there is no single `Row_ID` or audit trail that survives across stages, and there is no mapping/include-drop/load-gating layer before data reaches a database at all. Clients' raw files vary widely in structure (merged cells, multi-row headers, hidden sheets, inconsistent types) and in content (duplicate rows, broken formulas, trend anomalies, mismatched categorical values), and today's tooling catches a subset of these per-script rather than as one accountable pipeline.

## Goals & Non-Goals

### Goals
- Detect all structural issues (S-01–S-28) before any value is read as data, and remediate the ones that change a table's shape (S-01–S-10); the rest are kept as stored and flagged for review, never silently dropped (Phase A).
- Detect and fix all cell-level type/format/value issues (D-01–D-25, D-26 new) with a full audit trail keyed by a permanent `Row_ID` (Phase B).
- Run all time-series / execution checks (N-06–N-15) and trend/outlier checks (N-16–N-20) as flag-only reports with an Analyst correction loop (Phases C, D).
- Map source columns and dimension values to a defined target schema, with an explicit per-attribute Include/Drop decision controlling what reaches the database (Phase E).
- Validate a mapped Parquet file against schema, grain, and DB-isolation/size rules before any load is permitted (Phase G).
- Load validated Parquet into DuckDB via a full-refresh (truncate + staged swap) model, never incremental append (Phase F).
- Give every phase an Analyst review workbook (download → edit → upload) with a consistent re-check-only-touched-rows contract.
- Let an Analyst supply a value-consolidation mapping file (e.g., grouping several raw category values such as vehicle types into one bucket value) at the start of a Clean run (D-26).
- Keep every phase spec self-contained — buildable from its own document plus the named old code files, without needing to read another phase's spec.

### Non-Goals
- Porting the old `data_quality_agent` codebase as-is — this is new design, reference material only.
- Incremental/delta loading into DuckDB — V1 is full-refresh only.
- SSO, multi-tenancy, RBAC, or other platform-level identity/project concerns — these belong to the wider platform's own specs, not this service.
- A target-schema editor UI, fuzzy column-matching method, or dimension-resolution algorithm — Phase E assumes a config file for the target schema and leaves the matching method as an open build decision (not yet designed).
- Chained ratio definitions (a ratio of two other ratios) in Phase D's N-18 — V1 assumes numerator/denominator are always raw mapped variables.

## User Personas
- **Analyst** — the primary user. Uploads client source files and config, reviews and corrects flagged findings in downloaded Excel workbooks per phase, re-uploads corrections, and makes the Include/Drop mapping decisions in Phase E.

*(No other personas are defined in the source material for this module; broader platform personas such as Tenant Admin or Business User belong to the platform-level specs, not this document.)*

## User Stories
- As an Analyst, I want every uploaded file's structure (headers, merged cells, hidden rows, multi-table sheets) automatically detected and corrected, so that downstream checks run against clean tables rather than raw layout noise.
- As an Analyst, I want every cell-level issue (type mismatches, bad dates, case inconsistencies, formula errors) flagged or auto-fixed with an audit entry, so that I can trust the cleaned data without re-checking it by hand.
- As an Analyst, I want to upload a value-mapping file at the start of a Clean run (e.g., to consolidate several vehicle-type labels into one bucket value), so that I can apply a deliberate categorical consolidation before any other hygiene rule runs.
- As an Analyst, I want time-series and trend anomalies (missing weeks, duplicate values, abnormal trends, availability mismatches) reported with enough detail to investigate, so that I can correct the source data or mark findings as valid.
- As an Analyst, I want to decide, per mapped attribute, whether it is included in or dropped from the database push, so that only the columns we actually want reach the warehouse.
- As an Analyst, I want a pre-load check that blocks a load when the data doesn't match its series' schema or grain, so that bad data never silently reaches the database.
- As an Analyst, I want every phase's findings available as a downloadable, editable, and re-uploadable Excel workbook, so that I can correct data directly rather than through a separate tool.

## Functional Requirements

| Phase | Name | Codes addressed | Core requirement |
|---|---|---|---|
| A | Source File & Structural Intake | S-01–S-28, E-12, E-13 | Recursively scan source files/sheets; detect true header/data regions, multi-row headers, merged cells, hidden/grouped rows and columns, broken named ranges, pivot/Excel-Table structures, sheet protection; produce one clean table per real data region with a permanent `Row_ID`, plus a full structural-findings log and Analyst review workbook. |
| B | Cell-Level Data Hygiene | D-01–D-25, D-26 (new), N-01–N-05 | Validate/normalize every value's type and format (numbers, dates, currency, whitespace, case, control characters); apply policy to Structure's formula/error annotations; detect near-duplicates, outliers, and exact-duplicate rows (tombstoned, never reused); apply an optional Analyst-supplied value-consolidation mapping file first, ahead of every other rule; produce cleaned Parquet plus audit and review workbook. |
| C | Time-Series / Execution Checks | N-06–N-15 | Flag-only checks over execution weeks, no-execution runs, week-over-week/month-over-month duplicates, duplicate variable data, duplicate/missing/out-of-order dates, weekly continuity, and duplicate variable names; produce `Data_Validation_Report.xlsx` with an Accept-via-correction review loop. |
| D | Trend & Outlier Detection | N-16–N-20 | Flag-only checks for abnormal per-variable trends (robust Z-score), period-over-period % change (MOM/QOQ/HOH/YOY) including arbitrary client-defined ratio metrics, Spend/Support availability mismatches, and spend concentration; every threshold is explicit per client with no hidden defaults; produce `Spend_Support_Trend_Report.xlsx`. |
| E | Column & Value Mapping | E-02, E-03, E-04, E-05, E-06, E-14, plus a new Include/Drop decision | Map source column names to a defined target schema; resolve dimension member values to canonical form; let the Analyst mark each mapped attribute Include or Drop, so only included attributes reach the Parquet that downstream phases read. |
| F | Fact Table Loading | E-07, E-08, E-09, E-15 | Load the mapped, validated Parquet into DuckDB per series via full-refresh (truncate + staged swap); only proceed when the pre-load gate report says `load_permitted: true`; no incremental append, no re-deduplication at load time. |
| G | Pre-Load Database Checks | E-10, E-11, E-16 | Validate a mapped Parquet's schema against the DuckDB target table, validate row conformance to the series' declared grain, and check DB-isolation (production vs. staging) and load-size sanity; write a `load_gate_report.json` per series that gates Phase F. |

## Non-Functional Requirements
- **Determinism:** identical input bytes and detected structure must produce identical `Row_ID`s and identical outputs across re-runs (Phase A's determinism/stability properties, §7).
- **Auditability:** every fix, drop, or tombstone across every phase is recorded as an audit entry keyed by `Row_ID`, never a silent change.
- **Data locality / privacy:** the pipeline runs locally — data stays on the local machine; the optional LLM assist (local Ollama, default `http://localhost:11434`) is loopback-only by default, keeps no prompts after a call returns, and only reads data at an Analyst-set privacy level.
- **Review-loop safety:** every phase's upload pre-check rejects an added, removed, or reordered row, or an edit to a read-only cell, before applying any review decision.
- **Load safety:** a production DuckDB target requires an explicit flag on the load job; an unexpectedly large load is a warning, not a silent pass-through.
- **Incremental reuse:** a table should be able to reuse its prior phase output when its upstream content hash and pinned config are unchanged, avoiding unnecessary recomputation.

## Success Metrics
*(Not defined in the source material — left blank pending stakeholder input.)*

## Dependencies
- **Storage/compute:** Parquet (intermediate and final output format), DuckDB (load target), Polars (table processing).
- **File handling:** lxml (Phase A's streaming workbook reader — calamine is deliberately not used, since mixing readers caused the old coordinate-shift defect), openpyxl (review workbooks).
- **Reporting:** ReportLab (PDF client report), Excel review workbooks per phase.
- **Optional LLM assist:** local Ollama server (candidate models benchmarked, e.g. `llama3.2:3b`, `qwen2.5:7b-instruct`), used in Phase A's structural review and Phase B's optional narrative summary.
- **Reference-only prior art:** `data_quality_agent` repo (`problem_detector.py`, `data_remediation.py`, `quality_report.py`), `Data_Validation_Report_fixed.py`, `Spend_Support_Trend_Report.py`, `combined_report_app.py` — named per phase for the technique they implement, not ported directly.
- **Config inputs per client:** Data File column mapping, Variable Mapping, Period Mapping, Data Check Specifications, Ratio Definitions (Phase D), target-schema config (Phase E — format not yet designed).

## Risks
- The uploaded pipeline diagrams ("New DQ Pipeline — Process Flow" / "High-Level Design") show MySQL as the load target, with a `pipelines.py`/Streamlit orchestration layer. **This is superseded by the specs: the confirmed architecture is Parquet → DuckDB** (Phases E, F, G), and the diagrams' MySQL/orchestration framing should not be built against.
- N-10's full pairwise variable comparison is O(n²) in variable count — may need a cheaper pre-filter at scale.
- N-18's ratio-definition model assumes no chained ratios; a client needing a ratio-of-ratios would require a redesign.
- Phase E's fuzzy-match method for column/dimension mapping (exact, edit-distance, or LLM-assisted) is undecided — affects both accuracy and the size of the unresolved-values review queue.
- Schema evolution (a new dimension column needing to reach the DuckDB target) has no defined process yet (Phase G).
- Native-Excel pivot/chart building in the old `Spend_Support_Trend_Report.py` (`win32com`) cannot run server-side and needs a real redesign, not a port.

## Open Questions
1. Where is Phase E's "target schema" defined — a config file per source kind, or something an Analyst builds once per client? Format not yet designed.
2. Does a Phase B value-mapping file (D-26) apply only to the run it's uploaded for, or can it be saved as a reusable per-client config?
3. Does a series-level schema/grain failure in Phase G block only that series' load, or the whole run (Open Question shared with Phase F)?
4. Should `Geography` remain a required Phase C config column given nothing in that phase currently reads it?
5. Should the Phase D Data Check Specifications sheet hard-require every threshold, or soft-allow a smaller active rule set as currently assumed?
6. Does Phase F need a way to load a specific prior Parquet version into DuckDB (e.g., for debugging), or is that out of scope for V1?
7. Who resolves a Phase G schema/grain failure in practice — does it route back to the Phase E review screen automatically, or is it a manual fix?
