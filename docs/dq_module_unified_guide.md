# Data Quality (DQ) Module: unified guide

Unified guide: what it does, how it works, what it covers, what is next

> **How to read this document**
> **Section 1** is the one-page picture. **Section 2** is how to start the app and find things in the repo.
> **Section 3** walks through each phase in order. **Section 4** is the table of every issue, the phase that handles it, and whether the code really does it.
> **Section 5** is everything still to do: next build steps, the future-work plan, and the Porter transformations not yet in DQ.


# 1. The module in one page

**Purpose.** Clients send messy Excel and CSV files. The module finds what is wrong with them, fixes what is safe to fix, flags everything that needs a human decision, and hands over clean tables with a complete record of every change. The long-term goal is to load that clean data into a database (DuckDB).

**Three rules the whole design follows**

- **Nothing changes silently.** Every fix shows the old value, the new value, the rule and where (file, sheet, row, column).
- **People decide, the system proposes.** Anything uncertain goes to a review queue; outliers and negatives are flagged, not guessed.
- **Same input, same output.** Phases B to D use no AI; only Phase A uses a local model, with a rule-based fallback.

## Phases and where each stands

| Phase | Name | What it answers | Status | Code |
|---|---|---|---|---|
| **A** | Structural intake | Where is the real table in this file? | Built | src/structure/ |
| **B** | Cell-level hygiene | Is every value clean, typed and consistent? | Built | src/hygiene/ |
| **C** | Time-series checks | Are the weekly series complete, ordered and non-repeating? | Built | src/timeseries/ |
| **D** | Trends and outliers | Are there suspicious jumps, ratios or spend concentration? | Built | src/trends/ |
| **Gate** | Client report and show-stopper stop | Can we continue, or must the client answer first? | Not built (flags are stored, no PDF) | - |
| **E** | Source-to-target mapping | Which warehouse column and dimension key is each source column? | Not built (spec exists) | docs/architecture/specs/dq_e_mapping.md |
| **G** | Database / schema checks | Is the target database and its schema right? | Not built (spec exists) | dq_g_db_checks.md |
| **F** | Fact-table load | Write the rows without duplicates or overwrites | Not built (spec exists) | dq_f_loading.md |
| **App** | Analyst web app and API | Upload, run, review, download | Built (no login yet) | src/api, src/orchestration, frontend/ |

Run order is A, B, C, D, Gate, E, **G, then F**: G only checks the database and must pass before F writes anything.


## How an analyst/client works through a run

1. **Upload** files or a folder (xlsx, xlsm, xls, xlsb, ods, csv, tsv, txt, parquet; 500 MB total).
2. For every phase: **Run, download the review Excel, edit, upload, then Accept and continue.** An uploaded workbook is checked first (same sheets, headers and Row_IDs) and only the changed cells are re-validated. Editing an earlier phase discards the later ones, because their input changed.
3. Phases C and D first need a **client config workbook** (pre-filled from the data; the analyst fills the yellow cells: thresholds, ratios, variable buckets).
4. At the end, **download the results**: one clean table per source (csv, parquet, xlsx), review workbooks, audit JSON, configs and one zip with SHA-256 checksums.
Only Phase A uses the local model (Ollama, llama3.2:3b). If Ollama is not running the app asks: check again, continue without the LLM, or cancel.


# 2. Getting started and where things are

| Task | How |
|---|---|
| Start the app | Run ollama serve in one terminal, then scripts\start.ps1; open http://127.0.0.1:8000. It binds to this machine only: there is **no login**, so do not expose the port. |
| Run tests | python -m pytest (about 5 minutes, starts real worker processes) and cd frontend && npm test. |
| Run one phase from the command line | python -m src.structure.cli <files> -o out_a, then python -m src.hygiene.cli run out_a -o out_run, then python -m src.timeseries.cli run out_run -o out_run --client-config <xlsx>, then python -m src.trends.cli run out_run -o out_run. |
| Settings | config/settings.yaml (thresholds, which fixes are on, service limits); config/run_inputs.yaml (expected columns, columns to check, city list, default currency and rates); config/city_names.yaml. |

| Folder | Contents |
|---|---|
| src/structure, hygiene, timeseries, trends | Phases A, B, C, D (one folder each, each with its own CLI) |
| src/common | What phases share: table loading, value parsing, cadence (daily/weekly/monthly), monthly-to-weekly roll-up, units, run inputs |
| src/orchestration, api, storage, security | The A-to-D state machine, worker processes, FastAPI endpoints (/api/v1), run folders, upload checks |
| frontend/ | React analyst app |
| docs/architecture/specs | One spec per phase (A to G); docs/dq_[a-d]_implementation_notes.md say what was actually built and where it deviates |
| docs/decisions/decision_logs.md | Why things are the way they are (read this before changing behaviour) |
| docs/open_items_and_next_steps.md, service_issues_and_roadmap.md, before_production.md | Open defects, security and platform gaps |


# 3. Phase by phase


## Phase A: Structural intake

**Goal.** Before any value is read as data, establish that the file, sheet and table region are read correctly.

**What it does**

- Scans uploaded files and folders (nested folders included) and lists every file and worksheet.
- Reads each workbook in a streaming way and records value-free structural facts (types, merges, hidden parts, filters, formulas) per file.
- Finds the real table(s): header row and multi-row headers, true start column, data region, footers, gap rows. A rule-based region finder proposes; the local LLM only reviews header/footer/table-cut decisions and can be overruled by checks.
- Applies structural fixes: drops title rows and empty columns, builds composite headers, unmerges and fills, splits several tables on one sheet, removes gap rows. Source Excel row numbers are kept (Row_ID).
- Reports Excel features that can hide or distort data: hidden rows/columns/sheets, outlines, filters, print areas, formatting that hides values, validation, named ranges, pivots, protection, comments, objects over cells.
- Reports tabs that look like one dataset split across sheets or files (S-09); the merge itself is left to later steps.
| Issues covered | S-01 to S-28, E-12, E-13. |
|---|---|
| **Output** | One clean table per real data region, Structural_Findings, and a full change log (audit.parquet). |
| **Analyst review** | Structural_Review.xlsx: Issues sheet (Warning / Fixed / Identified), Changes sheet, one sheet per table. Analyst can correct the header row or data range, tick which tables continue, or type a new column after the last one. |
| **Code** | src/structure/ (extract, header, regions, findings, features, review_workbook). Spec: dq_a_structural_intake.md. |
| **Good to know** | Memory is not bounded: very large sheets stop at a size limit. A re-upload re-runs the whole phase (unchanged files are reused). |


## Phase B: Cell-level data hygiene

**Goal.** With the structure clean, validate and normalise every individual value.

**What it does**

- Runs one rule per issue over each selected table, in a fixed order (headers, formulas/errors, encoding, control characters, spaces, null placeholders, multi-value cells, cities, dates, numbers, zeros, case, near-duplicates, units, negatives, outliers, precision, optional zero filling, duplicates, derived columns, date order, expected columns, column names, truncation).
- **Fixes** what is safe (spaces, null placeholders, currency symbols, date formats and serials, Excel errors, case, leading zeros, mojibake, exact duplicate rows). Every fix is logged and also listed for review, so Reject can undo it.
- **Flags only** what needs a decision (formulas with no result, external workbooks, cut-off text, negative values, cells holding several values, city spelling variants, dates out of order, missing expected columns, currency symbols, a column that is the sum of others).
- Produces no AI text: the summary is built from counts.
| Issues covered | D-01 to D-25 (except D-26, not built), D-27 to D-33 (new), N-01 to N-05, E-01. |
|---|---|
| **Output** | Value-clean typed tables (Parquet), quality report per table, remediation_audit.json, needs_review.json, buckets.json. |
| **Analyst review** | Hygiene_Review.xlsx: Summary, Needs_Review (Decision: Accept / Reject / New Value / Split), Auto_Fix_Log, and the data with changed cells in colour. Analyst can also add a column after the last one; it is checked on upload. |
| **Code** | src/hygiene/ (rules.py is the rule list, kpi.py, cities.py). Fix switches: hygiene.fixes in settings.yaml. Spec: dq_b_cell_hygiene.md. |
| **Good to know** | **D-14 (cap outliers) and D-27 (fill an isolated zero) start switched off in the app.** Capping and filling rewrite a time series that C and D then check; with D-14 on, Phase D found 5 of 14 planted issues instead of 14. D-12, D-14 and D-22 do change values (the original spec said never); D-15 only flags. |


## Phase C: Time-series checks

**Goal.** Validate the weekly execution structure that is specific to this data.

**What it does**

- Reads the client config workbook (Data File, Variable Mapping, Period Mapping, optional Data Check Specifications and Column Setup).
- Checks each series (one combination of the label columns, e.g. city x channel) rather than the whole table. Cadence (daily, weekly, monthly) is read from the dates.
- Counts valid execution weeks; finds runs of 9 or more zero weeks (the 9 is configurable); finds repeated non-zero values week-over-week and month-over-month (one finding per run); finds variables with identical data; checks duplicate and missing dates, date order, 7-day gaps and duplicate variable names.
- Monthly tables are first turned into weekly ones (a week crossing a month end is one row; sums are split by days, averages are day-weighted).
| Issues covered | N-06 to N-15 (also re-reports N-01 to N-05 per variable). |
|---|---|
| **Output** | Data_Validation_Report.xlsx: Overall_Summary, Errors, WOW_Duplicates, MOM_Duplicates, No_Executions, Duplicate_Variable_Data, Variable_Checks, Date_Integrity, Execution_Weeks, Data. |
| **Analyst review** | Mark a finding 'Valid – no change', or correct the Data sheet (edit, add a missing week, delete a duplicate). Phase C then re-runs on the corrected table. |
| **Code** | src/timeseries/. Spec: dq_c_time_series.md. |
| **Good to know** | Needs a date column and a numeric measure; otherwise the app explains why and offers **Skip Phase C**. N-07 counts numeric zeros only. Geography is required in the config by default (timeseries.require_geography). |


## Phase D: Trend and outlier detection

**Goal.** Find values that are formally valid but statistically suspicious in the time series. Nothing is changed.

**What it does**

- Rolls weeks up to months, quarters, half-years and years using Period Mapping.
- N-16: robust z-score (median / MAD) per variable. N-17: MOM / QOQ / HOH / YOY % change against configured thresholds, both sequential and same-period-last-year.
- N-18: % change of client-defined ratios (e.g. CPM). N-19: spend present without support or the reverse. N-20: spend concentration in the top tactics.
- For monthly tables, also writes a Monday-to-Sunday weekly table split by days in each month (extra output only).
| Issues covered | N-16, N-17, plus N-18, N-19, N-20 (new). |
|---|---|
| **Output** | Spend_Support_Trend_Report.xlsx (change sheets, outlier details, trend chart, concentration), findings.json. |
| **Analyst review** | Status per flag: Confirmed Issue / Valid Movement / Corrected; values can be corrected in the data sheet; Phase D re-runs. |
| **Code** | src/trends/. Spec: dq_d_trends.md. |
| **Good to know** | **No default thresholds.** A rule without its threshold does not run and is listed as INACTIVE. Needs Spend/Support pairs in Variable Mapping. |


## Gate: client report and show-stopper check (not built)

Intended: collect every needs-review and client-action item from A to D, build the client PDF, and either stop until the client answers or continue with caveats. Today the items exist (findings carry a client_report flag) but no PDF and no stop/proceed rule is built.


## Phases E, G, F: mapping and loading (not built)

The specs are written; no code exists yet. The specs target **Parquet to DuckDB**, not MySQL as the original workflow document said.

| Phase | What it will do | Rule |
|---|---|---|
| **E Mapping** | Map each source column to a target column (exact names only), resolve each value to a dimension member, report unresolved values and source columns with no target, and mark each column Include or Drop. The mapping is saved per client. | E-02 to E-06, E-14 |
| **G Pre-load checks** | Check the Parquet against the live DuckDB schema, the declared grain of each series, and that the target is the right (non-production) database and of sane size. Writes load_gate_report.json, which F must obey. | E-10, E-11, E-16 |
| **F Load** | Full-refresh load in one transaction (no incremental append, so no row-alignment or duplicate risk), no second de-duplication, post-load NULL check. Always needs a person to confirm. | E-07, E-08, E-09, E-15 |

> **Where this guide differs from the original workflow document**
> **Database:** DuckDB (specs E to G), not MySQL. **Entry point:** the web app and orchestrator, not pipelines.py/Streamlit.
> **Phase B** uses no LLM at all (removed 11 Oct). **Phase C** checks per series, not one whole-table stream; N-08/N-09 give one finding per run, not per pair.
> **Phase D** is fully specified: N-16 is a robust z-score, and there are three new checks (N-18 to N-20). Thresholds have no defaults.
> **New rules not in the old list:** S-25 to S-28 and D-27 to D-33. **Still missing:** D-26, the Gate, and Phases E, G, F.
> **Hidden rows/columns and sheet protection** are only reported, never a blocker: the data is always read in full.


# 4. Issues addressed, by phase, checked against the code

Every row below was checked in the repository (rule function, finding code or config). **Fixes** = changes data and logs it. **Flags** = reports or sends to review, data left as it is. **Restructures** = Phase A changes the table layout. **Not built** = specified but no code.

| ID | Issue | Phase | In the repo | What the code does |
|---|---|---|---|---|
| **Phase A: Structural intake** |  |  |  |  |
| **S-01** | Headers not at row 1 | A | Restructures | Header row detected; rows above set aside as placements. Uncertain choice raises a warning. |
| **S-02** | Headers not at column A | A | Restructures | Table starts at its own first column; leading empty columns not carried. |
| **S-03** | Multi-row headers | A | Restructures | Group and unit rows attached to columns; composite names only for blank or duplicate labels; ambiguous cases warned. |
| **S-04** | Data at arbitrary location | A | Restructures | Table range found by the region finder, Excel Table range, or override. |
| **S-05** | Empty columns | A | Restructures | Columns with no label and no data dropped; header-only and sparse columns kept and reported. |
| **S-06** | Sparse sheet (over 50% empty) | A | Flags | Reported; extraction is region-based so only populated tables come out. |
| **S-07** | Multiple tables in one sheet | A | Restructures | Each table written separately (t1, t2 ...). |
| **S-08** | Aesthetic gap rows | A | Restructures | Empty rows between data rows removed; source row numbers kept. |
| **S-09** | Data scattered across tabs | A | Flags | Reports tabs or files that form one layout family; merge is not done here. |
| **S-10** | Merged cells | A | Restructures | Data merges unmerged and filled; header merges become group labels; title merges untouched. |
| **S-11** | Hidden rows | A | Flags | Read anyway; warning if the hidden rows hold data. |
| **S-12** | Hidden columns | A | Flags | Read anyway; warning if hidden columns hold data. |
| **S-13** | Grouped / outlined rows | A | Flags | Kept with their level; reported. |
| **S-14** | Grouped / outlined columns | A | Flags | Kept with their level; reported. |
| **S-15** | Frozen panes | A | Flags | Used as a header hint; reported only when it disagrees with the header found. |
| **S-16** | AutoFilter active | A | Flags | All rows kept; filtered rows marked; warning when a filter hides rows. |
| **S-17** | Print area restricting data | A | Flags | Reported; all data included. |
| **S-18** | Formatting hiding values | A | Flags | Detects font-equals-background and ';;;' formats; reported. |
| **S-19** | Data validation dropdowns | A | Flags | Rules checked against cells; violations reported. |
| **S-20** | Broken named ranges | A | Flags | Classed #REF!, missing sheet, external or empty; reported. |
| **S-21** | Pivot table remnants | A | Flags | Reported; a pivot sheet is never auto-selected. |
| **S-22** | Excel Table objects | A | Restructures | Table range used as the boundary; totals row excluded. |
| **S-23** | Sheet protection | A | Flags | Reported; uploaded file is never modified. |
| **S-24** | Special characters in sheet names | A | Fixes | A sanitised key is used for references; reported. |
| **S-25 (new)** | Hidden / very hidden sheets | A | Flags | Read; tables on them are never auto-selected. |
| **S-26 (new)** | Formulas without result, external links, #REF!, recalculate-on-open | A | Flags | Reported; blocks when too many results are missing in a column. |
| **S-27 (new)** | Charts / images covering data | A | Flags | Reported with the covered range. |
| **S-28 (new)** | Cell comments and notes | A | Flags | Stored as annotations, shown in review, never applied to a value. |
| **E-12** | Files and sheets across nested folders | A | Fixes | Folder uploads scanned recursively; every sheet inventoried. |
| **E-13** | Metadata-first extraction | A | Partial | Value-free structural facts are saved per file; no separate metadata-only export (that remains a utility script). |
| **Phase B: Cell-level data hygiene** |  |  |  |  |
| **D-01** | Numbers formatted as dates | B | Fixes | Date in a numeric column restored to its Excel serial number. |
| **D-02** | Invalid / bogus dates | B | Fixes | Feb 30 clamped to the last real day; month 13 or bad year becomes NULL plus review. |
| **D-03** | Inconsistent date formats | B | Fixes | Rewritten in the column's most common format. |
| **D-04** | Mixed data types in a column | B | Fixes | In a numeric column (80% or more numbers) text becomes NULL plus review; a 50-80% column is only flagged. |
| **D-05** | Formula vs value confusion | B | Flags | Formula with no stored result sent to review; cannot be rebuilt. |
| **D-06** | External formula references | B | Flags | Reported to the client; cannot be fixed by code. |
| **D-07** | Broken formula references | B | Fixes | #REF! becomes NULL; original logged. |
| **D-08** | Excel error values | B | Fixes | #N/A, #VALUE! etc. become NULL; original logged. |
| **D-09** | Leading / trailing whitespace | B | Fixes | Trimmed; double spaces collapsed. |
| **D-10** | Case inconsistencies | B | Fixes | Normalised to the column's most common spelling. |
| **D-11** | Header trailing spaces | B | Fixes | Stripped from column names. |
| **D-12** | Identifier variations | B | Fixes | Near-identical names merged into the most frequent spelling; placeholders (test, dummy, 9999) become NULL. Changed from 'report only'; Reject undoes it. |
| **D-13** | Special characters | B | Fixes | Control and non-printable characters become a space. |
| **D-14** | Outliers / entry errors | B and D | Fixes (off in app) | Rare outliers capped per series (switch off by default in the app). The time-series view of outliers is N-16 in Phase D. |
| **D-15** | Negative values / missing minus signs | B | Flags | Any numeric column with negatives gets one item for the client; nothing is changed. Replaces the old 'missing minus sign' check. |
| **D-16** | Lack of documentation | B | Fixes | Columns renamed from an optional data dictionary; cryptic names (col_7) sent to the client. No screen for the dictionary yet. |
| **D-17** | File corruption / encoding errors | B | Fixes | CSV tried as utf-8, cp1252, latin-1; unreadable files listed. (Phase A also reports unreadable and password-protected files.) |
| **D-18** | String 'NA' vs real null | B | Fixes | NA, N/A, null, None, - become NULL unless it is the column's top value at 5% or more. |
| **D-19** | Duplicate rows | B | Fixes | Exact duplicates dropped (first kept, logged); same key with different values goes to review. |
| **D-20** | Encoding issues (mojibake) | B | Fixes | Garbled text re-decoded; BOM dropped. |
| **D-21** | Numeric precision loss | B | Fixes | Columns over 15 digits or id-like kept as text. |
| **D-22** | Inconsistent units | B | Fixes | Mixed percentage forms and dollars-vs-cents converted to the majority unit; each change reviewable. |
| **D-23** | Truncated values | B | Flags | Values cut at the same length reported to the client. |
| **D-24** | Leading zeros stripped | B | Fixes | Padded to the column's usual length, with evidence or for ZIP/PIN columns. |
| **D-25** | Date as serial number | B | Fixes | Converted to a date using the Excel epoch. |
| **D-26 (planned)** | Value consolidation upload (group raw categories into one bucket) | B | Not built | In the PRD only. |
| **D-27 (new)** | Isolated zero / blank in a dated series | B | Opt-in | Single gap with both neighbours present filled with their mean; off by default; never touches zero-is-valid columns. |
| **D-28 (new)** | Column that is the sum of other columns | B | Flags | Total flagged as derived; buckets.json says what to add and what not to. |
| **D-29 (new)** | More than one value in a cell | B | Flags | Lists and ambiguous commas flagged; Split only on an explicit answer. |
| **D-30 (new)** | Same city written differently | B | Flags | One item per spelling with the standard name proposed. |
| **D-31 (new)** | Dates out of order in a series | B | Flags | Reported; sorting is left to the transformation step. |
| **D-32 (new)** | Expected column missing | B | Flags | Compared with the run inputs, nearest column named. |
| **D-33 (new)** | Currency symbol without a currency code | B | Flags | Read in the default currency (INR); other symbols flagged. |
| **N-01** | Number / text validity | B (and C) | Fixes | Shares D-04 / E-01; Phase C repeats it per variable. |
| **N-02** | Missing / blank cells | B (and C) | Fixes | Blank text becomes NULL. |
| **N-03** | NULL / N/A / None | B (and C) | Fixes | Shares D-18. |
| **N-04** | Spaces | B (and C) | Fixes | Shares D-09. |
| **N-05** | Special characters | B (and C) | Fixes | Shares D-13. |
| **E-01** | Currency symbols and thousands separators | B | Fixes | Removed before conversion. |
| **Phase C: Time-series checks** |  |  |  |  |
| **N-06** | Count of valid execution weeks | C | Flags | Counted per variable; shown on Execution_Weeks. |
| **N-07** | No executions (9 or more zero weeks) | C | Flags | Runs listed on No_Executions; the 9 is configurable. |
| **N-08** | Week-over-week duplicates | C | Flags | Repeated value per series, Sequential or Not Sequential; one finding per run. |
| **N-09** | Month-over-month duplicates | C | Flags | Same, on monthly totals. |
| **N-10** | Duplicate variable data | C | Flags | Variables with identical non-zero series. |
| **N-11** | Duplicate dates | C | Flags | Date_Integrity sheet. |
| **N-12** | Missing dates | C | Flags | Missing weeks against the expected calendar. |
| **N-13** | Date order | C | Flags | Strict chronological order within a series. |
| **N-14** | Weekly continuity (7-day gaps) | C | Flags | Gaps and overlaps listed. |
| **N-15** | Duplicate variable names | C | Flags | Ignores case and spacing. |
| **Phase D: Trends and outliers** |  |  |  |  |
| **N-16** | Abnormal trend identifier | D | Flags | Robust z-score (median / MAD) per variable; needs 5 or more values. |
| **N-17** | YOY / QOQ / HOH / MOM % change | D | Flags | Against configured thresholds; no defaults, rule is INACTIVE without one. |
| **N-18 (new)** | Ratio % change (CPM-style) | D | Flags | Client-defined ratios with their own threshold. |
| **N-19 (new)** | Spend / support availability mismatch | D | Flags | One present without the other. |
| **N-20 (new)** | Spend concentration | D | Flags | Top tactics holding too much of spend. |
| **Phases E, G, F: mapping and loading (all not built)** |  |  |  |  |
| **E-02** | Source column vs target schema mismatch | E | Not built | Spec: exact names only. |
| **E-03** | Foreign key / dimension mapping | E | Not built | Value to dimension member to key. |
| **E-04** | Unresolved dimension values | E | Not built | Block the load; list for review. |
| **E-05** | Wrong dimension attribute chosen | E | Not built |  |
| **E-06** | Source attribute with no target column | E | Not built | Treated as a schema gap. |
| **E-14** | Exact target naming | E | Not built |  |
| **E-11** | Schema validation before loading | G | Not built | Parquet vs live DuckDB schema. |
| **E-16** | Reporting granularity / grain | G | Not built | Grain per series checked before load. |
| **E-10** | Independent database / load size | G | Not built | Non-production target; size sanity. |
| **E-07** | Source-to-fact row alignment | F | Not built | Resolved by design: full refresh. |
| **E-08** | NULLs in existing fact columns | F | Not built | Post-load check. |
| **E-09** | Duplicate-row risk when loading | F | Not built | Resolved by design: no incremental append. |
| **E-15** | Loading without unnecessary de-duplication | F | Not built | Dedup happens once, in Phase B. |


# 5. What is next


## 5.1 Finish the pipeline (in suggested order)

1. **Tidy the open defects** (commit pending work; one row per column and rule in Needs_Review with an 'apply to all' answer; decide require_geography; per-table re-run on a Phase A review upload).
2. **Phase E mapping** with a saved mapping file per client; unresolved values block the load. Decisions needed: who owns the target schema and dimension lists, per client or per source kind, and whether a repeat run applies the saved mapping silently.
3. **Phases G then F:** the gate first, then a full-refresh DuckDB load in one transaction.
4. **Gate and client report PDF** after Phase D.
5. **Platform:** login and tenants, durable state and queue, Job Contract for B to D, retention, metrics. Do not host on a shared server before this.

## 5.2 Future-work plan (from DQ Module Future Work, 21 Oct)

**The idea.** Reduce analyst effort by putting ownership of data quality with the client: the client sees what is wrong with their data and answers for it; the analyst reviews the answers instead of chasing every issue. The target flow is Upload, Checking, Client answers, Analyst review, Client sign-off, Loaded, ending in clean data, a sign-off record and a proof record (every question, answer, approval and change with who and when).

| Topic | What is planned | Status in the repo |
|---|---|---|
| **Client and analyst roles** | Client uploads, answers each flagged item (keep, correct, explain, ask) and signs off; analyst approves or overrides; a client can assign work to another user. Missing: how answers are collected, whether Excel download/upload is allowed and how the sheet is protected. Walk through sample files first. | Not started. The app is analyst-only, local, no login, no sign-off or proof record. Excel round trips exist per phase. |
| **Appending new data** | Each new file is appended and the **combined** data checked: overlap (same period, different values), duplicates, gaps, new or missing columns, scale or currency change. Decision made: never replace an old value without recording who asked for the overwrite; a change in overlaps affects the model. | Not built across files. Within one file: D-19 duplicates, N-12 gaps, D-32 columns, D-22 units. |
| **Mapping file** | Client may add extra columns or a lookup table linked by a key (e.g. campaign to campaign type), possibly prepared with their own Claude. Validate before use, every key must be found, unknown keys become flagged items, saved per client, source of file visible to the analyst. | Partial. Analysts can type extra columns into the review workbooks (checked on upload). Lookup join and per-client saved mapping are not built (Phase E). |
| **Units and scale** | Record the unit (currency and ones/thousands/millions) for every numeric column, suggest it from the header, flag a scale change between files, show the unit beside numbers. Decisions made: keep data as in the source file, convert when files are integrated, record the choice in config. | Partial. D-22 (mixed units in a column); D-33 and run_inputs store default currency and rates but **no module converts yet**. No unit capture or UI display. |
| **Large data** | Plan for up to about a million rows (confirm: rows or value?). Show counts and samples, never every row; group flags by rule and column with 'apply to all'; Parquet plus a database; runs of minutes with a progress indicator (time estimate later) and an email when questions are ready. | Partial. Parquet outputs and live progress exist. Grouped 'apply to all' review and email are not built; D-29 and D-15 already use column-level items. |
| **UI needs** | Old vs new value everywhere; a correction path where the client proposes and the analyst approves a change request; numbered items with outcomes; a stage bar per file; one thread per item; units beside numbers. | Partial. Old/proposed value and decision exist in Needs_Review and Auto_Fix_Log; a phase stepper exists. No change-request approval, threads or per-file stages. |
| **LLM as assistant** | Narrow jobs only: turn a flag into a plain question, draft answers from past choices, suggest units and mappings, summarise, turn a request into a change request. It must never edit data, accept a phase, sign off, load, state numbers itself or follow text found in data. | Not built beyond Phase A (and Phase B's LLM was removed). Plan in docs/agent_build_plan.md, with autonomy levels 0 to 2. |
| **MCP** | One controlled door for the in-app assistant, an analyst's or a client's own assistant: tools (create run, start phase, list items, record answer, request change, approve) and resources (run state, findings, proof record); approve and load never available to the model without a person. | Not built. The /api/v1 endpoints are the base. |


### Moving from ADS to DQ

The ADS build step cleaned data inside the model-building flow and replaced values on its own. In DQ, **flag first, change only after a decision**; no change is automatic.

| # | What ADS did | In DQ | Status in the repo |
|---|---|---|---|
| **1** | Monthly figure put in the week holding the 1st | Split across weeks in proportion to days | Done. Monthly-to-weekly before Phase C (weekly_rollup) and as an extra Phase D output (Weekly_Allocation.xlsx). |
| **2** | Ignored zeros when averaging | Treat zeros by variable type | Partial. Analyst ticks 'zero is a valid value' per column; Column Setup sheet chooses sum or average roll-up. A full variable-type tag (business KPI, zero-expected, rate, sum) is not built. |
| **3** | Filled an empty week with the order-weighted average of its neighbours | Business KPIs only, as a flagged item applied after approval | Partial. D-27 fills a **single** isolated zero or blank with the plain mean of both neighbours; opt-in, with Accept/Reject. Order weighting and KPI-only typing are not built. Mostly-zero (50%) and mostly-blank (30%) columns are skipped; the future-work note suggests a configurable share such as 10%. |
| **4** | Replaced weeks beyond 5 standard deviations | Flag and highlight only; replace only after talking to the data provider; skip weather columns; add a period-wise ratio test | Partial. N-16 flags; D-14 capping is off by default. Weather exclusion not built; N-18 client-defined ratios exist, but the exact period-wise ratio test is still to be defined. |
| **5** | Media de-duplication 'not possible' | A gate that flags overlap between the three buckets, with a client question | Not built; the three buckets were never listed. |
| **6** | Harmonisation between sources | Later, when two sources report the same measure | Not needed yet. |

**Extra check to add:** proxy metrics must be present together (for Google Ads, spend, impressions and clicks cannot be zero or null in only one of them). N-19 checks spend/support pairs only, not this three-way rule. **Missing-value rules** from the note: one missing day is imputed from neighbours (sum or average roll-up), zero where expected (weather, impressions, spend) is not imputed, and spend without impressions is checked with the data provider. When many values are missing or zero, suggest loading complete data instead of imputing.

**Still to decide:** whether rule 3 (fill gaps) clashes with 'zero expected' on the same column (the variable type should decide), the three overlapping buckets for item 5, the period-wise ratio definition, and whether 'millions' means rows or value.


## 5.3 Porter data transformations: not yet part of DQ

The Porter codebase (52 Python scripts) holds the business transformations the old team ran by hand. Most are **data transformation**, not data quality, and we have **not yet decided where or how they will be built**. Some of their pre-conditions are already flagged by DQ. Per the decision log, sorting and similar reshaping stay in a later transformation step, not Phase B.

| Area (scripts) | What it does, simply | Status |
|---|---|---|
| **Influencer (influencer_processor/main.py, main(2).py)** | Classify influencers by genre and follower size; split spend, reach, views, likes and comments across cities using each city's audience percentage (grain: date x campaign x genre x city). | Not taken up |
| **Coupon (coupon_main.py, coupon_city_split.py, coupon_classify.py)** | A cell with several cities is split into one row per city and the usage shared out (integer split keeps the total); coupons classified by vehicle, discount type, band and condition from hard-coded dictionaries. | Flagged in DQ: multi-city cells (D-29) and city names (D-30). Splitting and classification not taken up |
| **Affiliate (affiliate_cumulater, affiliate_channel_others, affiliate_city_split)** | Sum impressions, clicks and installs per day/channel/network; group unknown channels into 'Others'; share the totals across cities by date and network. | Not taken up |
| **Reactivation (reactivation_crosstab.py)** | Turn Day 1 to Day 31 columns into dated rows and spread inactivity buckets into columns (6,120 x 36 became 68,319 x 10). | Not taken up |
| **OOH (src/ooh/step1, step2, main; ooh_spend_negative.py)** | Summarise spend by month; a negative in the next month is the return of this month's spend, so final spend = allocation + next month's negative. | Flagged in DQ: negative values per column (D-15). Netting not taken up. ooh_spend_negative.py looks like an older duplicate |
| **DAP (dap.py)** | Append newer DAP data after 8 Sep 2025 to the history; add City from 15 supplied Geo IDs (others 'unspecified'; appended rows keep their city name, Geo ID blank). | Idea stage: city shown as an ID, plus date-overlap checks when new data arrives (see 5.2) |
| **Revenue (revenue_generator_aov.py)** | Completed orders per date, city, vehicle and segment times the matching average order value gives revenue_amount; two sheets, one new column. | Not taken up |
| **Utilities: dollar removal, date sorting, city name formatting, Head Office to Bangalore, USD to INR, two-sheet merge** | Small clean-ups and conversions. | Dollar and currency symbols: E-01 removes, D-33 flags. Date order: D-31 flags, sorting not done in DQ. City names: D-30 flags. USD to INR: rates are stored in run inputs, not applied. The rest not taken up |
| **Mapping and discovery (platform_finder, product_type_finder, extract_source_metadata, populate_competitor_search)** | Find platform/channel mappings, list product types, read file/sheet/column metadata, fill mapping keys in the database. | Outside DQ: extension of city mapping and helper files (relates to Phase E) |
| **Google Ads and Meta Ads mapping engine (main, mapper, matcher, schema, analytics)** | Append extra columns to raw rows from a supplied mapping file: pick the best join key, match on a normalised key, fuzzy-match unmatched campaigns, write mapped, unmapped and quality reports. Google Ads campaign-type and vehicle-type columns were made **by hand with Claude**. | Outside DQ: merging two sheets cannot be automated here; missing values and multi-city cells are already checked by DQ |
| **Analysis and report scripts (missing, outlier, weekly, crosstab, old-vs-new comparison, PDF)** | Diagnostics rather than source transformations. | Largely replaced by Phases B to D. Old-vs-new comparison relates to the appending checks in 5.2 |

> **Open questions for the transformation work**
> Where do these live: a separate transformation module after DQ, or inside Phase E mapping? What triggers them (config, client, source kind)?
> Which are repeatable rules (affiliate sums, reactivation unpivot, OOH netting) and which stay manual (Google/Meta mapping columns)?
> The Porter notes say date sorting is 'done'; the repo only flags order (D-31), so sorting is still open.
> Exact script versions and run dates are not confirmed in the code (influencer, coupon, affiliate, revenue); verify before relying on them. The DAP script's final run should be re-verified.


## 5.4 Known gaps to remember

- No login, TLS or tenant separation; run state lives on one disk; no retention policy for data/runs.
- Phase A: LLM overruled correct rule answers in the live test; large sheets are limited by memory; one spec contradiction (distinct vs counted group labels) needs a decision.
- Phase B: D-26 (value consolidation) and the data-dictionary screen are not built; one review row per flagged cell can be very long.
- Phase C: designed for weekly data; daily and monthly meaning of each check still to be decided. No daily-to-weekly roll-up exists yet (sum, or average ignoring zero rows).
- Web app: UI not final; review is by Excel round trip; polling instead of streaming; browser-level tests missing.
Companion detail: docs/dq_[a-d]_implementation_notes.md, docs/dq_service_implementation_notes.md, docs/open_items_and_next_steps.md, docs/decisions/decision_logs.md, and the specs in docs/architecture/specs/.
