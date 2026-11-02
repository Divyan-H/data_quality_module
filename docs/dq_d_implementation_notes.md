# Phase D implementation notes (`src/trends/`)

Spec: [dq_d_trends.md](architecture/specs/dq_d_trends.md) (v0.3), built from `Spend_Support_Trend_Report.py`.
Tests: `pytest tests/test_trends.py`. Planted sample: `python tests/fixtures/build_phase_d_workbook.py`.

## Run it

Pre-filled client config (as Phase C's `init-config`): the data tells us the Period / Variables columns, each
variable's Spend/Support type, the Support -> Spend pairing, Bucket and the Period Mapping labels. Green = filled from
the data (Autofill_Note says how), yellow = you enter it: the thresholds, Ratio Definitions, Aggregation 1 (N-20
grouping) and anything the names could not decide. Nothing is locked; no threshold is invented (§3.6).

```
python -m src.trends.cli init-config samples/phase_d_weekly.xlsx -o my_config.xlsx     # then fill the yellow cells
```

Phase D alone, on one file (as the reference script):

```
python -m src.trends.cli run samples/phase_d_weekly.xlsx -o out_d --client-config samples/config/phase_d_client_config.xlsx
python -m src.trends.cli review out_d out_d/phase_d_trends/Spend_Support_Trend_Report.xlsx
```

After Phase C: `python -m src.trends.cli run out_run -o out_run` (no `--client-config`: Phase C's copy of the shared
workbook is used). `run` takes a Phase C run folder or one csv / xlsx / parquet file.

**Shared code.** Table loading, writing, value rendering and the Data File lookups live in `src/common/` (also used by Phase C); Period dates are read by one `Periods` class for Phases C and D (Phase D had its own reader; on 43,000 generated inputs it gave the same dates as Phase C's, so they were merged). Phase D shares only the file contract with Phases A to C: `tables/<ref>/data.parquet` with a `_row_id` column, plus `run.json`. A loose file gets the same table ref and Row_IDs Phase A would give it.

Output under `<run>/phase_d_trends/`: `Spend_Support_Trend_Report.xlsx`, `findings.json` (status open | confirmed |
valid | corrected, `client_report` for the Gate), `run.json` (incl. `rules` and `inactive_rules`), `tables/<ref>/data.parquet`
(Phase E's input), `client_config.xlsx`, `review.json`, `review_audit.json`, `review_versions/`.

## Choices worth knowing

- **No default thresholds (§3.6).** A rule without its threshold does not run; `Rules_Used` lists it INACTIVE and is
  shown (not hidden) whenever a rule is inactive or a specification row is not used (e.g. the old `CPM % change`).
  Suggested service-wide pattern: `run.json` `rules` + `inactive_rules`, and a WARNING line in `pipeline.log`.
- **Everything runs on the configured axis (§3.1)**: values summed per Period date; dates not in Period Mapping are
  dropped and reported on `Unmapped_or_Missing`. The reference ran N-16 / N-19 per row on all dates.
- **Period labels come from Period Mapping** (N-17 / N-18 too, spec §2), so fiscal calendars work. Same period last
  year = the label holding the same dates a year earlier (majority vote); no such data -> comparison omitted (§3.3).
- **N-18** generalizes CPM: `Ratio Definitions` rows, own threshold, sequential + same-period-LY. Chained ratios and
  unknown variables are a `ConfigError`; a ratio on an N-row or absent variable is INACTIVE.
- **Trend Line without Excel (§6 note 1):** static table + openpyxl line chart (Spend on secondary axis) that follows
  the Performance Variable filter. The Data-Model pivot is not built; `Spend_Concentration` holds the same ranking.
- **Review:** `Confirmed Issue` / `Valid Movement` / `Corrected` (workflow doc). Marks are pre-filled in the next
  download and can be cleared. `Corrected` + still flagged -> open again with a note; no longer flagged -> kept as
  `corrected` in findings.json. Finding ids include the values, so changed data is asked again.

## Checked against the reference script

On `samples/phase_d_weekly.xlsx` (minus its off-axis row) the four `*_Changes` sheets, `Outlier_Details`,
`Spend_Support_Availability`, `Spend_Concentration`, `Spend Skewness` and `Unmapped_or_Missing` match the reference
row for row; ratio "TV CPM" reproduces its CPM % change. Known reference behaviour kept: partial first/last periods and
4- vs 5-week months move MOM totals (~25%).

## Found while testing: Phase B's D-14 removes Phase D's findings

Run A -> B -> C -> D on the planted sample and Phase D reports 5 findings instead of 14. Phase B's outlier fix
(`hygiene.fixes.D-14: true` in settings.yaml) caps exactly the planted cases before Phase D sees them: Radio_GRPs
676.67 -> 220, Search_Spend 4770 -> 770, Social_Impressions 0 -> 72000. That removes the abnormal-trend (N-16),
period-change (N-17), concentration (N-20) and availability (N-19) findings behind those values.
Phase C's notes describe the same effect. Phase D is not changed for this. For weekly time-series inputs, set
`hygiene.fixes.D-14: false`, so Phase B only proposes the cap on Needs_Review.

## Spec open questions

1. Chained ratios: not supported (ConfigError). 2. N-19 magnitude threshold: not added. 3. Soft requirement kept.
Not done: wiring into `pipelines.py`, the Gate and Streamlit (later build steps).


## Monthly data also comes out as weeks (added 2026-10-21)

For a table whose time level is **monthly** (`src/common/granularity.py`), Phase D also writes a Monday-to-Sunday weekly table
(`src/common/monthly_weeks.py`, `src/trends/weekly.py`): `phase_d_trends/weekly/<table>/` with `tables/<ref>_w/data.parquet`,
`Weekly_Allocation.xlsx` (rule, one line per monthly row, the weekly table) and `meta.json`. Results lists it as `<table>_weekly`.

* Every numeric measure column of a monthly row is split in proportion to the days each week has **in that month**. A week that
  crosses a month end is two rows (one per month); a month that starts on a Saturday (August 2026) has a first row of 2 days = 2/31
  of the month. The last row of a month takes the remainder, so the rows add back up to the monthly figure (checked per row and
  reported; a row that does not is listed).
* Added columns: `Week_Start` (the Monday), `Segment_Start`, `Segment_End`, `Days`, `Days_In_Month`. Labels repeat on each row.
  Because a straddling week appears twice, `Week_Start` is not unique; use `Segment_Start`.
* Extra output only: Phase D's checks, report, findings and thresholds do not read it, it is written for every monthly candidate
  table (also those the client config does not describe), and it is rebuilt after a Phase D review upload. Setting
  `trends.monthly_to_weekly: false` turns it off. If Phase D is skipped no weekly table is made.
* Not done: a daily-to-weekly roll-up (sum, or average ignoring zero rows). Nothing in Phases A-D rolls daily data up.
