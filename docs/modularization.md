# Modularization: how results are stored, resuming, and what to split next

Review of the current code, 2026-10-23 (about 15,500 lines in `src/`). Nothing in this note has been changed in the code; the opportunities are proposals.

## 1. Short answers

| Question | Answer |
|---|---|
| How are intermediate results stored? | On disk, one self-contained folder per run. Each phase writes its tables as **Parquet** (one table per folder, with a `_row_id` column), plus `run.json`, findings and audit files as **JSON**, and its review workbook as **Excel**. The run's progress is one `state.json` and an append-only `events.jsonl` |
| Can I resume any step if the earlier ones are done? | **Yes, at phase level.** A phase starts when the one before is accepted, and each phase can also be run alone from the command line on the earlier phase's folder (or on a plain csv, xlsx or parquet file). **No, inside a phase:** a failed or interrupted phase starts again from the beginning. Phase A is the only one that reuses finished work (unchanged files) |
| Is there a need or opportunity to modularize further? | Yes. The phase interface is already clean, but Phases C and D copy the same helper code, `common` depends on `hygiene`, there is no package metadata, and the new weekly roll-up is recomputed on every load instead of being stored as a step (section 6) |
| How are utility functions built? | As a shared package, `src/common`, imported by every phase. It is a plain package inside the repository, not a separately installed library. Each phase also has its own small helper modules (section 5) |

## 2. How the code is layered

| Layer | Package | Responsibility | Knows about |
|---|---|---|---|
| Web and API | `src/api` | HTTP routes (`/api/v1`) and the legacy job endpoints | orchestration only |
| Orchestration | `src/orchestration` | The state machine A to D, worker processes, review and config uploads, results bundle | phase adapters, storage |
| Phase adapters | `src/orchestration/phases` | One adapter per phase with the same methods: `run`, `review`, `clear`, `summary`, `artifacts`, `candidates`, `make_template`, `check_config` | its phase package |
| Phase packages | `structure` (A), `hygiene` (B), `timeseries` (C), `trends` (D) | The rules, checks, reports and review loop of one phase. Each has its own command line | `common` |
| Shared code | `src/common` | Table loading and writing, value parsing, period reading, granularity, additive columns, weekly conversion | nothing (see 6.1) |
| Services | `src/storage`, `src/security`, `src/llm_interface` | Run folders and events, upload checks, the local model probe | nothing above them |

The adapter is the only code that knows how to call a phase. Adding a later phase (mapping, load, checks) means writing one more adapter and one more phase package.

## 3. How intermediate results are stored

One run is one folder under `data/runs/<id>/`. Copying the folder keeps the run: state, inputs, every output and the audit trail.

| Path | Written by | Format | Holds |
|---|---|---|---|
| `state.json` | the API process only (atomic replace, one lock per run) | JSON | Phase statuses, options, uploads, the current job |
| `events.jsonl` | the worker processes (append only) | JSON lines | Live progress and log lines shown in the UI |
| `inputs/` | upload | original files | What the client sent |
| `phase_a/` | Phase A | Parquet and JSON | Per file: cells, audit, sheets, tables; `inventory.json`, `report.json`, LLM decisions, review workbook |
| `phase_a_state/` | Phase A | JSON | Structure config and cell edits kept across review uploads |
| `work/phase_b_hygiene/` | Phase B | Parquet, JSON, Excel | `tables/<ref>/data.parquet`, `run.json`, quality reports, needs-review and audit files, `Hygiene_Review.xlsx` and its versions |
| `work/phase_c_timeseries/` | Phase C | Parquet, JSON, Excel | `run.json` (index of checked tables) and one folder per table: report workbook, `findings.json`, the table itself, `weekly_rollup.json` when the data was monthly |
| `work/phase_d_trends/` | Phase D | same pattern | Report, findings, review marks, weekly extra output |
| `config/` | the analyst | Excel | Pre-filled and uploaded client config workbooks |
| `uploads/` | the analyst | Excel | Every review workbook uploaded, versioned |
| `summaries/` | orchestrator | JSON | A cached summary per phase for the UI |
| `jobs/<id>/` | runner | JSON and logs | `request.json`, `result.json`, worker log of each process |
| `final/` | orchestrator | csv, parquet, xlsx, zip | Exports built for the Results page |

**The contract between phases is a folder, not a function call.** A phase reads the previous phase's folder: `tables/<ref>/data.parquet` with a `_row_id` column, plus its `run.json`. Row IDs and table references stay the same through all phases, which is how a finding can be traced back to a source cell. Phase D reads Phase C's table when Phase C ran (after any correction the analyst uploaded) and Phase B's table when Phase C was skipped, or when Phase C worked on a weekly table made from a monthly one.

## 4. Resuming

| Situation | What happens today |
|---|---|
| Start phase X after X-1 is accepted | Normal path. X reads X-1's folder from disk |
| Run one phase alone | `python -m src.<phase>.cli run <previous phase folder or a loose file> -o <folder>` for B, C and D. Works with no web app |
| Start a phase again | Its own output and **every later phase's output are deleted** (`_clear_from`), because their input changed. Config uploads stay but are marked "check again" |
| Service stops while a phase runs | On startup a run left "running" is marked failed ("interrupted"), the stray worker is killed, and the analyst starts the phase again |
| A phase fails halfway | Marked failed; start it again. The folder may hold partial output until then (known issue B5 and B6 in the roadmap note) |
| Review workbook uploaded | The phase re-runs on the corrected data; uploads are versioned and audited. Phase A reuses unchanged files by content hash if the rule pack, config and local-model pin are the same |
| Copy a run folder to another machine | Works. One weak spot: `run.json` stores the absolute input path of the machine that ran it |

**Not available:** resuming from the middle of a phase. At millions of rows a failed Phase C or D check repeats all tables. Phase A has per-file reuse; B, C and D do not have per-table checkpoints, although each already works one table at a time and writes each table's folder separately, so the pieces exist.

## 5. How utility functions are built

**Shared package `src/common`:**

| Module | What it provides |
|---|---|
| `values.py` | Classify and parse a cell, read periods (`Periods`), month labels, name cleaning |
| `dataset.py` | The `Dataset` table object, loading a phase's output or a loose file, writing a table, column lookup by letter or name |
| `columns.py`, `errors.py`, `logs.py` | System column names, `ConfigError`, run loggers |
| `granularity.py` | Finds the date column, the cadence (daily, weekly, monthly...), and whether a phase applies; derives a date from Year-Month labels |
| `additive.py` | Finds columns that add up to another |
| `monthly_weeks.py`, `weekly_rollup.py` | Splits monthly figures over weeks (two-row form for Phase D; one row per week, sum or average per column, for Phase C) |
| `units.py` | The index file for runs with several tables. **The name is misleading**: it has nothing to do with measurement units |

**Phase-local helpers** sit next to the code that uses them (`hygiene/values.py` for cell parsing, `timeseries/layout.py`, `trends/values.py`, `orchestration/phases/base.py` for folder paths, events and errors, `security/uploads.py` for file-name and size checks).

**What is not there:** the old template stubs (`utils/helpers.py` and similar) were removed. There is no `pyproject.toml`, so `common` is imported as `src.common` from the repository root and cannot be installed on its own. Shared code is tested through the phases (`tests/test_granularity.py`, `test_weekly_rollup.py` and the phase suites), not through its own suite.

## 6. Opportunities

### 6.1 Fix the layering
`src/common/values.py` and `src/common/granularity.py` import from `src/hygiene/values.py` (`parse_date`, `has_keyword`, `EXCEL_EPOCH`). The shared package therefore depends on one phase. Move those functions into `common`; then `common` depends on nothing.

### 6.2 Remove copies between Phases C and D
Found by search, identical or near-identical in both packages:

| Duplicated | In |
|---|---|
| `_Sheet`, `read_sheet`, `_filled`, `norm`, `load_settings` | `timeseries/config.py`, `trends/config.py` |
| `ReviewError`, `keep_version` (and the pre-check pattern) | `timeseries/review.py`, `trends/review.py`; a third `ReviewError` in `hygiene/review.py` |
| `GEO_WORDS`, `SPEND_WORDS`, `SUPPORT_WORDS`, `tokens`, `stem`, `var_type` | `timeseries/autoconfig.py`, `trends/autoconfig.py` |
| The pre-filled workbook writer, partly | the same two files |

Proposed homes: `common/config_sheets.py` (reading a config workbook), `common/review_files.py` (`ReviewError`, versioned copies), `common/names.py` (word lists and name helpers). The word lists are also an input candidate: one list, editable.

### 6.3 Give the run folder a real interface
Every phase writes its own paths by hand. A small `PhaseFolder` class (read table, write table, read and write `run.json`, list versions, mark complete) would replace the path building in each phase and in `bundle.py`. It would also let each adapter declare its inputs and outputs, so the orchestrator can check "can this phase run" from what is on disk, not only from `state.json`. Rename `units.py` to `table_index.py` before the measurement-unit feature uses the word.

### 6.4 Per-table checkpoints
Phases B, C and D already process one table at a time. Marking each finished table (a `done` flag in its folder) would let a failed or cancelled phase skip the tables already finished. This matters most at millions of rows.

### 6.5 A named "prepare" step before Phases C and D
Conversions that happen before a check (derive a date from Year-Month labels, monthly to weekly) are now called inside the loaders (`timeseries/io.load_candidates`, `trends/io._tables`). The weekly table is recomputed each time the candidates, the config check and the run load it, and it is stored only as Phase C's output after the checks. A prepare step that writes its own result (and its log) would make it inspectable, resumable and cheaper.

### 6.6 Split the large files
Over 500 lines each: `structure/extract.py` (800), `structure/pipeline.py` (645), `trends/checks.py` (561), `orchestration/orchestrator.py` (551), `hygiene/rules.py` (522), `structure/xlsx.py` (518). The check modules could be one file per check family (value checks, no-execution, repeats, date integrity), and the orchestrator could separate "state machine" from "uploads and config handling".

### 6.7 Make the boundaries enforceable
Add package metadata (`pyproject.toml`), install `common` as its own distribution (needed once the mapping, load and checks phases or a second service use it), and add an import-rule check to CI (for example `import-linter`) so a phase cannot import another phase.

### 6.8 Later, with the platform work
Move `state.json` and the job table to a database and the folders to object storage. The folder contract in section 3 maps onto this directly if the `PhaseFolder` interface (6.3) hides where the files live.

## 7. Suggested order

| Step | Change | Risk | Effort |
|---|---|---|---|
| 1 | 6.1: move the three functions into `common` | Low, mechanical, covered by tests | Small |
| 2 | 6.2: merge the duplicated C and D helpers | Medium: two phases change at once | Medium |
| 3 | 6.3: `PhaseFolder` and the `table_index` rename | Medium: touches every phase's paths | Medium |
| 4 | 6.5: persist the prepare step | Low | Small |
| 5 | 6.4: per-table checkpoints | Medium | Medium |
| 6 | 6.6, 6.7: split large files, packaging, import rules | Low | Medium |

Each step should be its own change with the full test suite green before the next. The daily and weekly data paths must stay byte-for-byte unchanged by every step; a regression test on a daily and a weekly file belongs at the start of step 2.
