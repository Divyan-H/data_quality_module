# Phase C implementation notes (`src/timeseries/`)

Spec: [dq_c_time_series.md](architecture/specs/dq_c_time_series.md) (v0.2), built from `Data_Validation_Report_fixed.py`
as the workflow document says. Tests: `pytest tests/test_timeseries.py`.

## Run it

```
python -m src.structure.cli samples/phase_c_weekly.xlsx -o out_a                       # Phase A
python -m src.hygiene.cli run out_a -o out_run -c config/settings.yaml --no-llm        # Phase B
python -m src.timeseries.cli run out_run -o out_run --client-config samples/config/phase_c_client_config.xlsx -c config/settings.yaml
python -m src.timeseries.cli review out_run out_run/phase_c_timeseries/Data_Validation_Report.xlsx -c config/settings.yaml
```

`run` takes a Phase B run folder (its `phase_b_hygiene/tables/*/data.parquet`) or one csv / xlsx / parquet file
(Phase C alone, as the reference script: header on the first non-empty row). It checks **one table**: the one named
with `--table REF` (patterns allowed), the only table, or the only table the client config describes (a Period
column of mostly dates and at least one mapped variable). Several matches is an error that lists them.
`review` exits with code 2 and lists every problem when an upload fails the pre-check; a config problem exits 1.

Output, under `<run>/`:

```
pipeline.log                          one line per run / upload (shared with the other phases)
phase_c_timeseries/
  Data_Validation_Report.xlsx         the report (below); the analyst's download
  findings.json                       every finding, one per line: id, code, status (open | valid), variables,
                                      periods, row_ids, reason, its sheet row, client_report
  run.json                            table, layout, counts per code (open / valid), PASS/FAIL of N-11..N-15
  tables/<ref>/data.parquet           the checked table; after an upload, the corrected one. Phase D reads this.
  client_config.xlsx                  copy of the client config workbook the run used (uploads re-use it)
  accepted.json                       findings marked "Valid – no change" (kept across runs and uploads)
  review_audit.json                   every upload's changes: cells edited, rows added / deleted / reordered, marks
  review_versions/                    every downloaded and uploaded report
```

## Calling it from `pipelines.py`

```python
from src.timeseries.pipeline import run_timeseries                   # Phase C
from src.timeseries.review import apply_upload, ReviewError
from src.timeseries.config import ConfigError, load_settings

res = run_timeseries(run_dir, run_dir, client_config_xlsx, "config/settings.yaml")   # reads Phase B's output
# Phase D reads run_dir/phase_c_timeseries/tables/*/data.parquet; the Gate reads findings.json
# (status "open"; client_report: true for the codes in timeseries.client_codes).
apply_upload(run_dir, uploaded_xlsx, load_settings("config/settings.yaml"))           # analyst review loop
```

## Client config workbook

The same workbook as Phase D. Sheet and column names are matched without regard to case, spaces or punctuation.

| Sheet | Columns | Used for |
|---|---|---|
| Data File | Description, Column; rows Period, Geography, Variables | Column is a letter of the checked table (A = its first column) or a header name; Variables = that column and every later one |
| Variable Mapping | Variables (or Variable / 3.3), Bucket, Description | a variable is checked only with a usable Bucket (not blank, 0, #N/A, N/A, NA, NULL, NONE) |
| Period Mapping | Period (or Date), Months | the month label shown on MOM_Duplicates; a date not in the sheet gets e.g. *January '24* |
| Data Check Specifications *(optional)* | Description or Rule, From, To, No of Weeks | date ranges for the No Executions / Week on Week / Month on Month rows; No of Weeks overrides N-07's 9 |

Every problem is a `ConfigError` naming the sheet and what to fix: a missing sheet or column, a letter beyond the
table, a Period column without dates, a From / To that is not a date, a No of Weeks below 1.

## Files

| File | Spec component (workflow document, Phase C table) |
|---|---|
| `config.py` | Config Reader: settings defaults (`timeseries:` in settings.yaml) and the client config workbook |
| `io.py` | reading Phase B tables / loose files with their header text; writing the checked table |
| `layout.py` | Data File -> columns; Variable Selector's table choice |
| `checks.py` | Variable Selector, Execution Checks, Duplicate Checks, Date Checks: one method per check |
| `report.py` | Report Writer: `Data_Validation_Report.xlsx` |
| `review.py` | Download -> Edit -> Upload: pre-check, data corrections, "Valid – no change" marks |
| `values.py` | numeric validity, period dates, finding ids |
| `pipeline.py`, `cli.py` | entry points |

## Coverage

| Code | Where | Sheet |
|---|---|---|
| N-06 | `run` (execution counts) | Errors (Execution Weeks), Execution_Weeks |
| N-07 | `no_executions` | No_Executions |
| N-08 | `wow` | WOW_Duplicates |
| N-09 | `mom` | MOM_Duplicates |
| N-10 | `duplicate_data` | Duplicate_Variable_Data |
| N-11, N-12, N-13, N-14 | `date_checks` | Date_Integrity, Overall_Summary |
| N-15 | `duplicate_names` | Variable_Checks, Overall_Summary |
| N-01 .. N-05 | `value_checks` (the reference report's own checks; Phase B's codes) | Variable_Checks, Errors |

The report has the reference's seven sheets plus **Variable_Checks** (N-01..N-05 and N-15, one row per variable and
check, so each can be marked valid), **Date_Integrity** (one row per duplicate date, missing week, out-of-order row,
irregular gap - the workflow document's "Date Integrity" sheet; the reference only counted them), **Execution_Weeks**
(N-06 for every variable, checked or not - spec §3.2 says always shown; the reference showed it only for variables with
an error) and **Data** (the checked table, cells in a finding yellow with a comment). Every findings sheet ends with
`Finding_ID` and `Review`.

`tests/fixtures/build_phase_c_workbook.py` builds a 60-week workbook with one planted case per check and its config
(`samples/phase_c_weekly.xlsx`, `samples/config/phase_c_client_config.xlsx`); Phase C must report exactly the 17
planted findings (`EXPECTED`). Run as a script it rebuilds the samples and runs Phase C into `out_phase_c/`.

## Checked against the reference script

`Data_Validation_Report_fixed.py` run on the two sample files gives the same WOW_Duplicates, MOM_Duplicates,
No_Executions, Duplicate_Variable_Data, Overall_Summary statuses and INFO values, Check_Index checks and Errors row
order. Errors differs in one place, a **bug in the reference**: `pandas.read_excel` turns the text `NULL`, `NA`, `N/A`,
`None`, `nan` into NaN before the script looks at it, so the reference reports them as Missing / Blank and its
NULL / N/A check can never fire. Phase C keeps them as text and reports N-03 (and N-01, N-05), as spec §3.1 says.

## Analyst review (spec §6)

Download `Data_Validation_Report.xlsx`, then either
- set **Review** to `Valid – no change` on a findings-sheet row: that finding is not reported again (any dash, case or
  spacing is accepted). The decision is kept in `accepted.json` and holds for later runs on the same run folder.
  A finding is identified by what it is about (variable, rows, dates, value), so it comes back if the data changes;
- or correct the **Data** sheet: edit values, add a row for a missing week (leave Row_ID blank), delete a duplicate
  row, sort by date. Every existing row must keep its Row_ID.

The upload is pre-checked (same sheets and headers, no Finding_ID added, removed or edited, Review values valid, no
unknown or repeated Row_ID). Phase C then runs again on the corrected table, which becomes the input of Phase D.
An unedited upload changes nothing (Excel's 15-digit floats and date cells compare equal to what was exported).

## Choices worth knowing

- **One finding per issue, with a stable id** (`N-08-<hash>`): pairs, runs, dates, names. The id hashes Row_IDs, not
  row positions, so sorting or deleting rows does not change the id of an untouched finding.
- **N-10 in one pass.** Variables are grouped by their non-zero (row, value) series instead of comparing every pair
  (spec open question 2); the result is the same as the reference's pairwise `equals`.
- **N-15 ignores case and spacing** (`TV Spend` / `tv  spend` are one name; the reference compared exact text). A
  Phase B table carries Phase A's unique names (`Spend__2`); the header text is recovered from them.
- **Letters win over names** in the Data File: `C` is column C even when a header is called `C`; a value that is not a
  letter of the table (`Week`, `TV`) is looked up as a header name.
- **Dates are ISO** (`2024-01-22`) everywhere in the report; the reference mixed `01/22/2024` and `2024-01-22 00:00:00`.
- **Text periods** are read month-first unless the column shows otherwise (`dayfirst: false`, pandas' default, as the
  reference); Excel date serials in the Period column are read as dates. Fully empty rows are skipped (the reference
  counted their cells as Missing / Blank).
- **Findings sheets list at most `max_sheet_rows` (100,000) rows.** N-08 is pairwise as the spec says, so a constant
  series of n weeks gives n(n-1)/2 pairs: 20 constant variables over 260 weeks give 673,400. Overall_Summary says how
  many were not listed; findings.json has all of them. A finding not listed cannot be marked valid in that round.
- **Performance** (400 variables x 260 weeks, 693k findings): 66 s, 5 MB report, 317 MB findings.json. The report is
  written in openpyxl's write-only mode (memory stays flat); a normal-size report takes about 0.15 s.

## Deviations and gaps

- **Rows can be added, deleted and reordered on upload.** The workflow document says Row_ID must be unchanged; Phase
  C's own fixes (fill a missing week, drop a duplicate date, sort) need row changes, so existing Row_IDs must be kept
  and new rows get `<table>:c<n>`.
- **One table per run.** The Data File describes one layout; a second table needs `--table` and its own run folder.
- **A letter means the table's column, not the original sheet's.** After Phase A moved a table (title rows, a leading
  empty column), name the columns in the Data File (`Period | Date`) or use the table's letters.
- **N-07 counts numeric zeros only**, as spec §3.2 and the reference do; the workflow document says "zero/blank".
- **Geography is required but not used** (spec open question 1); `require_geography: false` turns the requirement off.
  Overall_Summary shows how many distinct geographies the table has - checks run across all of them.
- A finding marked valid cannot be un-marked from the workbook (it is no longer listed): edit `accepted.json`.
- Wiring into `pipelines.py`, the Gate and Streamlit is not done: those are later steps of the build order.

## Found while testing: Phase B's D-14 fix feeds Phase C false findings

On the sample workbook run through Phase A -> B -> C, Phase B's outlier fix (D-14, switched on in settings.yaml)
caps each outlier to the nearest normal value, an existing value of the same column. In a weekly table that column is
the time series, so: the capped values become WOW duplicates of that value (TV_Spend: 2 planted pairs became 10), and
an isolated zero week (a real "no execution") is capped up (Radio_Spend 0 -> 300), which inflates N-06 and hid the
N-10 copy. For weekly time-series inputs, set `hygiene.fixes.D-14: false` (the outlier is then only proposed in
Needs_Review), or exempt zeros from D-14.

## Spec open questions - where the build stands

1. Geography: kept required (switchable), not used; see above.
2. N-10 cost: solved without a pre-filter - grouping by series is O(n) and exact.
3. N-15 scope: Variables range only, as the spec says; S-03-style whole-sheet duplicates stay with Phase A.
4. *New:* N-08's pairwise listing grows as n² on a constant series. One row per (variable, value) with its weeks
   would carry the same information; needs a spec decision because the spec says pairwise explicitly.
