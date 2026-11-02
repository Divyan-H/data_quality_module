# User inputs: what we collect from clients and analysts

Status of the current implementation, 2026-10-22. This note lists every input the pipeline takes from a person today, especially the Excel workbooks that are downloaded, filled in and uploaded back, then lists what we still do not ask for and should.

Input arrives in three ways:
1. **The data files** the client uploads.
2. **Excel workbooks**: the service writes one, the person edits it, and uploads it back. It is checked before anything runs.
3. **Choices in the app** (switches and ticks on a phase's start screen).

Today every input belongs to one run. **Nothing is saved per client**, so the same answers are asked again on the next run.

---

## 1. At a glance

| Phase | Input | Form | Filled by | Needed to continue? |
|---|---|---|---|---|
| Upload | Data files | Upload | Client | Yes |
| A. Structure | `Structural_Review.xlsx`: which tables to use, header rows, ranges, cell edits | Excel, round trip | Analyst | No (A's own choice stands) |
| B. Values | Fix switches and zero-expected columns | App | Analyst | No (defaults apply) |
| B. Values | `Hygiene_Review.xlsx`: a decision on each flagged value, and optionally new columns typed after the last column of a Data sheet | Excel, round trip | Analyst or client | No (items stay open) |
| C. Time series | Client config workbook (4 sheets) | Excel, round trip | Client with analyst | **Yes** |
| C. Time series | `Data_Validation_Report.xlsx`: mark or correct findings | Excel, round trip | Analyst or client | No |
| D. Trends | Client config workbook (up to 5 sheets, with thresholds) | Excel, round trip | Client with analyst | **Yes** |
| D. Trends | `Spend_Support_Trend_Report.xlsx`: a status per finding | Excel, round trip | Analyst or client | No |

---

## 2. Data files

- Accepted types: `.xlsx`, `.xlsm`, `.xls`, `.xlsb`, `.ods`, `.csv`, `.tsv`, `.txt`, `.parquet`. Old formats (`.xls`, `.xlsb`, `.ods`) are converted first.
- Whole folders can be dropped. Total size per run is limited (500 MB by default); an uploaded review or config workbook is limited to 200 MB. Both limits are in `config/settings.yaml`.

## 3. Phase A: `Structural_Review.xlsx`

Sheet **Structural_Findings**: one row per table found. The first ten columns are locked (what the pipeline found). The analyst edits the last seven:

| Column | What to enter | Example |
|---|---|---|
| Use | `Y` to keep the table, `N` to drop it | `Y` |
| Status | `table`, or `not a table` | `not a table` |
| Header rows | Which rows are the header, or `none` | `5,6` |
| Data range | The cell range the table covers | `A5:H900` |
| Exclude rows | Rows inside the range to leave out | `120-121` |
| Exclude columns | Columns inside the range to leave out | `G` |
| Note | Free text | |

- **Add a missing table:** 20 blank rows sit at the end; enter File, Sheet and Data range.
- **Correct a value:** each kept table has its own sheet (Row_ID in column A). An edited cell becomes a cell edit that is applied on every later run.
- **Checked on upload:** locked columns must be unchanged, ranges and row lists must be valid, and the sheet set must match. Problems are listed by sheet and cell.

## 4. Phase B

**In the app (before the run):**

| Input | What it does | Default |
|---|---|---|
| Automatic fixes on or off (about 25 rules, D-01 to N-02) | Off means detect only; the change is proposed for review | All on except D-14 (cap outliers) and D-27 (fill an isolated zero) |
| Columns where zero is a real value (`zero_expected`) | Those columns are not treated as having missing values | None |
| Data dictionary (optional file: column name, standard name) | Renames columns to standard names (D-16) | None. A setting only, no screen |

**In `Hygiene_Review.xlsx`:**

| Sheet | Editable | Meaning |
|---|---|---|
| Needs_Review | **Decision**: `Accept`, `Reject` or `New Value` | Accept keeps the proposed change, Reject restores the original |
| Needs_Review | **New_Value** | Required for `New Value`; only for single-cell items |
| Data_1, Data_2 … | Any value | Changed cells are green; cells waiting for a decision are yellow, each with a comment |
| Auto_Fix_Log, Summary, Tables | Nothing | Read-only record (old value, new value, reason) |

Upload is checked: same sheets, headers and Row_IDs; the read-only columns must be unchanged; a Decision must be one of the three words. Every exported and uploaded version is kept.

## 5. Phase C: client config workbook

A pre-filled copy is offered for download (`phase_C_client_config_prefilled.xlsx`). The client or analyst checks it and uploads it back. It is validated first, and the run cannot start without it. Sheet and column names are matched ignoring case, spaces and punctuation.

| Sheet | Columns | What it says | Required |
|---|---|---|---|
| Data File | Description, Column | Which column holds the **Period**, the **Geography** and the first **Variable** (a letter such as `C`, or a header name). Variables are that column and every one after it | Yes. Geography required unless switched off in settings |
| Variable Mapping | Variables, Bucket, Description | The bucket of each variable. A variable with a blank, `0`, `N/A` or similar bucket is not checked | Yes |
| Period Mapping | Period, Months | The month label of each date (used for the month-on-month duplicate check) | Yes |
| Data Check Specifications | Description (or Rule), From, To, No of Weeks | Date ranges for the no-executions, week-on-week and month-on-month checks, and the length of a run of zeros that counts as a finding (default 9) | Optional |

**Review workbook `Data_Validation_Report.xlsx`:** on any finding row, set **Review** to `Valid – no change` and it is not reported again. The analyst may also edit values on the Data sheet, add a row (leave Row_ID blank: a missing week), delete a row (a duplicate date) or reorder rows (dates out of order). Phase C then runs again on the corrected data and writes every change to an audit file.

## 6. Phase D: client config workbook

Same workbook layout as Phase C, with these sheets (Phase D has its own pre-filled copy):

| Sheet | Columns | What it says | Required |
|---|---|---|---|
| Data File | Description, Column | Period and Variables (Geography is not used) | Yes |
| Period Mapping | Period, Months, Quarters, Half Years, Full Years | The label of each date for month, quarter, half-year and year comparisons | Yes |
| Variable Mapping | Variables, Spend Variables, Y/N, then optional Description, Bucket, Aggregation 1 | Pairs of a Support variable and its Spend variable. Only `Y` rows run; at least one is needed | Yes |
| Data Check Specifications | Rule (or Description), Threshold | One threshold per row (below). **Blank means the rule does not run** | Optional |
| Ratio Definitions | Ratio Name, Numerator, Denominator, Threshold | A ratio to track period on period. Numerator and Denominator must be mapped variables | Optional |

**Thresholds in Data Check Specifications:** Abnormal Trend (robust Z-score, for example 3.5); MOM, QOQ, HOH and YOY % change (enter 30 for 30%, not 0.30); Spend Concentration top-tactics count (a whole number) and its percentage (needs both).

**Review workbook `Spend_Support_Trend_Report.xlsx`:** each finding gets a **Review**: `Confirmed Issue`, `Valid Movement` (not a data problem) or `Corrected` (fixed on the Data sheet).

## 7. What happens when a workbook comes back

- The upload is checked against what was exported: sheets, headers, Row_IDs and locked columns. If anything is wrong, **nothing is applied** and every problem is listed with its sheet and row.
- A phase that depends on the upload re-runs on the new data.
- Uploads are versioned and every mark or change is written to an audit file for the run.

---

## 8. Inputs we do not ask for yet

These are decided by the code today, or are not available at all. Each should be a column or sheet in the workbooks above, then saved per client.

| # | Input | Today | Where it should be asked |
|---|---|---|---|
| 1 | **Unit and scale per numeric column** (currency; ones, thousands, millions) | Not recorded. Currency symbols are stripped. Rule D-22 guesses "cents among dollars" | New sheet **Column Setup**, with a suggestion from the header ("Spend in $M") |
| 2 | **Roll-up type** (sum, average, rate) and **variable type** (business KPI, zero-expected) | Guessed from words in the column name. The monthly-to-weekly split divides every numeric column, including averages and rates | **Column Setup** |
| 3 | **Zero-expected columns** | Ticked in the app each run | **Column Setup**, saved |
| 4 | **Weight column for a gap fill** (for example completed orders) | The only gap fill (D-27) uses a plain mean and is off by default | **Column Setup** |
| 5 | **May an outlier be replaced?** | Flag only, or a fixed cap (D-14) | **Column Setup**: flag only, or replace after the data provider agrees |
| 6 | **Week start day and fiscal year start** | Monday to Sunday and calendar years are fixed in code | New sheet **Calendar** |
| 7 | **Expected frequency** of each table | Guessed from the gaps between dates | New sheet **File Settings** |
| 8 | **How to read dates** (day first or month first) | Phase B day first, Phases C and D month first | **File Settings**, one answer for all phases |
| 9 | **Is Geography required?** and which columns are dimensions | Required by default; columns guessed from names | **File Settings** and **Column Setup** |
| 10 | **Rule for new data that overlaps old data** | Not built | **File Settings** |
| 11 | **Mapping file** to the target columns, and lookup tables | Not built | New sheet **Mapping File** |
| 12 | **Sign-off** name and email | Not built | In the app, at sign-off |

## 9. Saved per client

Today the Phase C and D workbooks, the Phase B switches and the zero-expected ticks are kept with one run only. A **client profile** should store the filled workbook, the Column Setup, Calendar and File Settings answers and the thresholds, so the next upload starts pre-filled and only differences are asked.

## Appendix: values fixed in the code that belong in a config file

Settings in `config/settings.yaml` already cover limits, Phase B rule switches and thresholds, and Phase C and D defaults. These are still in the code:

| Value | Where |
|---|---|
| Gap fill accepts only daily or weekly series; 80% of cells must be readable dates; 60% of steps regular; at most 60 labels, 12 row-wise measures | `hygiene/kpi.py` |
| "Cents among dollars" bounds (5% and 50%); 95% uniqueness for an ID column | `hygiene/rules.py` |
| Cadence ranges for daily, weekly, monthly, quarterly, yearly | `timeseries/checks.py` |
| At least 5 non-zero values for the abnormal-trend test | `trends/checks.py` |
| Repeat-run lengths (2 and 3), ignore-bucket words and null tokens: exist as settings but are not in `settings.yaml` | `timeseries/config.py` |
| Additive-column search limits | `common/additive.py` |
| Display limits (300 preview rows, 400 issues, 100,000 workbook rows); file safety limits (50 million cells per sheet, 5 GB package) | `orchestration`, `structure/xlsx.py` |
| Fallback secret for row fingerprints | `structure/pipeline.py` |

Also inconsistent: the local-AI review mode default (`all` in `settings.yaml`, `near_ties` in the Phase A defaults), and the model name written in three files.
