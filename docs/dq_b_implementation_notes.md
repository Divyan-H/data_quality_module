# Phase B implementation notes (`src/hygiene/`)

Spec: *New DQ Pipeline - Workflow Document*, Phase B and section 4 (module `hygiene/`). Tests: `pytest tests/test_hygiene.py`.

## Run it

```
python -m src.structure.cli samples -o out_a                                  # Phase A
python -m src.hygiene.cli run out_a -o out_run -c config/settings.yaml   # Phase B
python -m src.hygiene.cli review out_run out_run/phase_b_hygiene/Hygiene_Review.xlsx -c config/settings.yaml
```

`run` takes a Phase A output folder or a single csv / xlsx / parquet file. From a Phase A folder it reads only the
tables Phase A **selected** (`inventory.json` → `selection`), with their `cells.parquet` annotations, layout id and
file/sheet names. Options: `--sheet NAME`, `--table REF...` (fnmatch patterns), `--all-tables` (ignore the selection).
Given an `.xlsx` directly (Phase B alone, no Phase A), every non-empty sheet is one table: the first non-empty
row is the header, empty rows are skipped, dates and numbers are rendered as Phase A does, and formula cells are
annotated (no stored result, external link, `#REF!`) so D-05/D-06/D-07 still fire. Titles, multi-row headers or several
tables in one sheet need Phase A first.
`review` exits with code 2 and lists every problem when an upload fails the pre-check.

Output, under `<run>/`:

```
pipeline.log                          one line per table, totals, review uploads (shared with the other phases)
phase_b_hygiene/
  tables/<ref>/data.parquet           value-clean, typed (Int64 / Float64 / Date / String), same Row_IDs
  quality_reports/<ref>.json          per table: source file, sheet, counts by rule, column types and nulls, summary
  remediation_audit.json              every change: Row_ID, Source_Row, column, rule, old, new (tombstones for dropped rows)
  needs_review.json                   every flag: cell or column, why, suggested action, original / proposed value, status
  Hygiene_Review.xlsx                 Summary, Needs_Review, Auto_Fix_Log, Tables, Data_<n> (changed cells green, to-review yellow, comments)
  review_versions/                    every downloaded and uploaded workbook
  run.json                            tables done, skipped (not selected / filtered), unreadable, totals
```

## Calling it from `pipelines.py`

```python
from src.hygiene.pipeline import run_hygiene          # Phase B
from src.hygiene.review import apply_upload, ReviewError
from src.hygiene.config import load_config

res = run_hygiene(phase_a_out, run_dir, "config/settings.yaml", None, sheet=sheet)
# Phase C reads run_dir/phase_b_hygiene/tables/*/data.parquet; the Gate reads needs_review.json
# (items with client_report: true are the client questions).
apply_upload(run_dir, uploaded_xlsx, load_config("config/settings.yaml"))   # analyst review loop
```

## Issue workbook (end-to-end check)

`python tests/fixtures/build_phase_b_workbook.py` builds `samples/phase_b_issues.xlsx` (one table, 42 rows, 50 issues
planted in known cells), runs Phase A into `out_phase_b_issues/` and Phase B into `out_phase_b_issues_run/`, and writes
`samples/phase_b_issue_catalogue.xlsx`: per issue, what Phase B does, the planted value, expected and actual result,
PASS/FAIL. `tests/test_hygiene_workbook.py` runs the same check in pytest. To cover a new case, add a line to `PLANTS`.

## Files

| File | Spec component |
|---|---|
| `io.py` | loading with alternative methods for corrupt files (D-17), annotations, typed output |
| `rules.py` | Detectors + Auto-Fixer + Review Router: one function per rule, `RULES` is the run order |
| `table.py` | `set` (fix + audit, or propose when the fix is off), `flag` (review), `drop_rows` (tombstone) |
| `values.py` | locale-aware number parser, date parser, keyword matching |
| `report.py` | Quality Report (summary text built from the counts; Phase B uses no LLM) |
| `review.py` | Download -> Edit -> Upload: `Hygiene_Review.xlsx`, pre-check, decisions, re-check of changed cells |
| `config.py` | defaults, overridden by the `hygiene:` section of `config/settings.yaml` |

## Coverage

| Checklist id | Rule (`rules.py`) | Fix or flag |
|---|---|---|
| D-01 | `numbers_read_as_dates` | fix: date in a numeric column becomes its Excel serial |
| D-02 / D-03 / D-25 | `dates`, `date_serials` | clamp Feb 30; month 13 / bad year -> NULL + review; mixed formats -> dominant; serials -> dates |
| D-04, N-01, E-01 | `numbers` | in a numeric column (>= 80% numbers): currency and separators stripped (E-01), non-numbers -> NULL + review; a 50-80% numeric column is only flagged |
| D-05 / D-06 | `errors_and_formulas` | review only (uncached formula, external workbook); the message quotes the formula (Phase A keeps it in `cells.parquet` `detail`) |
| D-07 / D-08 | `errors_and_formulas` | `#REF!` and Excel errors -> NULL, original logged |
| D-09, N-04 / D-11 | `whitespace_and_blanks` / `headers` | trim, collapse, blank -> NULL (N-02) |
| D-10 | `case` | dominant spelling per case-insensitive group |
| D-12 | `near_duplicates_and_sentinels` | fix + review: near-identical names merged into the most frequent spelling; placeholder and 9999-style values -> NULL |
| D-13, N-05 | `control_chars` | non-printable -> space |
| D-14 / D-15 | `outliers` / `negative_values` | fix + review: a rare outlier (robust z, order of magnitude) is capped to the nearest normal value, a skewed column is only flagged. D-15 is flag-only (changed 2026-10-27): any numeric column with at least one negative value gets one column item for the client (how many, the first few and their Excel rows) and no value is changed; before that it flagged a cost/loss column with *no* negatives |
| D-16 | `column_names` | fix: renamed to the data dictionary's standard name; ambiguous names (`fd_burn`, `col_7`) or names missing from the dictionary -> client |
| D-17 | `io.load_tables` | csv tried as utf-8, cp1252, latin-1; unreadable files listed in `run.json` |
| D-18, N-02, N-03 | `placeholders` | NA / N/A / null / None / - -> NULL unless it is the column's top value at >= 5% |
| D-19 | `duplicates` | exact duplicates dropped (first kept, tombstone names the kept Row_ID); same key (an id column that is >= 95% unique), different values -> review |
| D-20 | `encoding` | mojibake re-decoded, BOM dropped |
| D-21 / D-22 / D-23 | `precision` / `units` / `truncation` | D-21 fix: column kept as text (> 15 integer digits, account/phone); D-22 fix + review: percentages to the majority form, cents among dollars / 100 (money columns); D-23 client flag only - cut text cannot be rebuilt |
| D-30 / D-31 / D-32 | `city_names` / `date_order` / `expected_columns` | flag only (added 2026-10-29), see the sections below |
| D-29 | `multi_value_cells` | flag only (added 2026-10-27), see "D-29" below: a cell that holds a list or an ambiguous comma is flagged with its separator, left exactly as it is, and held back from every later rule |
| D-24 | `leading_zeros` | pad to the column's modal length, only with evidence (a value keeps a leading zero) or for ZIP/postal/PIN columns |

`fixes:` in `settings.yaml` turns any fix off; the rule still runs and proposes the change in `Needs_Review` instead.
Every change is in the workbook: one Auto_Fix_Log row with its reason, a green (or yellow when it also needs a decision)
cell with a comment in the Data sheet, and a count on the Summary sheet.
Items on rules D-06, D-15, D-16, D-21, D-22, D-23, D-29 carry `client_report: true` for the Gate. D-05, D-06 and D-23 have no fix (no formula result, external workbook, cut-off text).

## Row numbers

Every `Row_ID` ends in the input file's real Excel row (`<file key>:<sheet>:t1:<row>`, a csv header counts as row 1); `Hygiene_Review.xlsx` shows it as `Excel_Row` on Needs_Review and Auto_Fix_Log and as `_excel_row` (column B) on every Data sheet. Phase B uses no LLM.

## Choices worth knowing

- **Phase A hands over text.** Every value is parsed here, per column, from the current values (nothing cached):
  a column is `date` if >= 50% of its values parse as dates, `numeric` if more values parse as numbers than not
  (a tie is text), `id` if its name has an id keyword (id/code/zip/phone...; never type-coerced).
- **Ambiguous numbers.** `1,234` is read as grouping unless the column's own other values show the comma is decimal.
  `1.234,56` and `99,99` are read as EU. Dates `a/b/yyyy` use the column's evidence, else `dayfirst`.
- **Review loop.** Accept applies a proposed fix if one exists (a switched-off fix), else only acknowledges.
  Reject restores a value that a rule nulled. New Value writes the text. A value edited directly in a Data sheet is
  an `analyst_edit`. Only the changed cells are re-validated; a bad New Value becomes NULL again with a new review item.
- **Upload pre-check** rejects changed sheets, headers, Row_IDs, row counts or read-only cells.

## Tuned on real data

Running all samples through Phase A then Phase B (79 selected tables) showed false positives on `test_data.xlsx`,
now fixed: a 74%-numeric category column was having its text nulled (D-04 now needs 80%), vehicle ids `1`/`8` were
padded to `01`/`08` (D-24 needs evidence), a repeated `vehicle_id` was treated as a key (D-19 needs a unique column),
snake_case names were called cryptic (D-16), long city lists and `HCV`/`LCV` were called near-duplicates (D-12 now
compares values up to 40 characters, and only typos within words of 4+ letters).

## Deviations and gaps

- N-01 "text columns hold valid text": a number inside a text column is valid text, so nothing is done there.
- D-16 "missing documentation" is only checked when `data_dictionary` is set; otherwise cryptic names are flagged.
- Needs_Review has one row per flagged cell, so a noisy column (e.g. many outliers) makes a long sheet.
- Wiring into `pipelines.py`, the Gate and Streamlit is not done: those are later steps of the build order.
- **Columns added by the analyst, after Phase A or Phase B (2026-10-22).** The spec says no column is ever added; this is a deliberate,
  analyst-driven exception. *After Phase B:* a new header typed directly after the last column of a `Data_<n>` sheet of
  `Hygiene_Review.xlsx`, with its values on the same rows, is accepted on upload (`review.new_headers`, `review.add_columns`). The
  header must be new and the column must have a value. The column is appended to the table and checked with the cell rules (everything
  except D-19 duplicate rows, D-27 zero filling and D-28 additive columns, which look across the whole table); its fixes and flags go to the audit and
  to Needs_Review, and an audit entry (`column_added`) records the addition. *After Phase A:* the same on a table sheet of
  `Structural_Review.xlsx` (the review sheets are not protected). Phase A does not put the column into its own table (that is built from
  the source cells and its checks assume it); the values are kept as `added_columns.json` in the Phase A folder, and Phase B joins them to
  the table on Row_ID when it loads it, so the normal Phase B run checks them. Not done: a lookup file joined on key columns.
- **Deviation from the spec, on request:** the spec says D-12 is never auto-merged and D-14 is never auto-fixed. Here D-12, D-14 and D-22 do fix values (switch off in `fixes:`); D-15 follows the spec and only flags, and every changed cell also goes to Needs_Review with its original value, so Reject undoes it.


## KPI preparation: D-27 and D-28 (added 2026-10-21)

Two rules that make the numbers safe to add up in the next modules (`src/hygiene/kpi.py`, `src/common/additive.py`).

* **D-27 isolated zero / blank values - opt-in.** Off, it does nothing. On (the "D-27" tick on the Phase B start screen), a zero or blank in
  a numeric column of a dated daily or weekly series (series = the table's label columns; cadence from the dates) is treated as a
  missing value **only when it is a single period with both neighbours present and one step away**, and is filled with the mean of the
  two. Every fill is a normal fix: `Auto_Fix_Log`, a green cell in `Data_<n>`, and a `Needs_Review` row (Accept keeps it, Reject restores
  the original, New Value replaces it). Never touched: columns the analyst ticked as "zero is a valid value" (`zero_expected`, sent from the
  start screen and kept in `run.json` and the Summary sheet), columns that are >= 50% zero or > 30% blank, runs of two or more periods
  (listed on `Needs_Review` as info), the first and last period, monthly or irregular series, and series with repeated dates.
  Rows that are missing altogether are not invented here; Phase C reports them (N-12). D-14 (outlier capping) runs first and would turn a
  zero into the column minimum, so it is off by default.
  *Why opt-in:* a filled zero is no longer visible to Phases C and D. On the planted weekly workbook it removes findings.
* **D-28 a column that is the sum of other columns - flags only.** Narrowed on 2026-10-22: only the case `Product A + Product B +
  Product C + Product D = Total`. From the numbers, not the names: a column equal to the sum of at least two other non-negative
  columns on >= 98% of the rows where all are filled (at least 6 rows). The total column is the one flagged ("a derived total, not a
  fact of its own"). Labelled rows (an "All India" row equal to the regions) and exact copies of a column are no longer looked for.
  Relations are reduced to the non-redundant ones and grouped into buckets with a role per member (total / intermediate / part). Each relation is a column-level item on
  `Needs_Review`; Accept confirms, Reject says it is a coincidence. `buckets.json` (artifact "hygiene-buckets") lists, per bucket,
  `sum_these` and `do_not_add_with_them`, with the decision. Switch off with `kpi_buckets: false`.
  Limits (reported as info items, never silent): negative columns are not searched; at most 40 columns per table; a node budget per
  search; the search runs on an even sample of 4,000 rows and every answer is then checked on all rows. A relation with
  `alternatives > 0` has other column sets that also fit and may be a coincidence.

## D-29: more than one value in a cell (added 2026-10-27)

`rules.multi_value_cells`, run after the null placeholders and before every date and number rule. It only flags: the cell keeps its
exact text, and `Table.held` makes the later rules (D-01, D-04, E-01, D-10, D-12, D-14, D-22, the date rules and the typed output)
skip it, so nothing is converted, capped or merged behind the flag.

| Column kind | What is flagged | Answers on `Needs_Review` |
|---|---|---|
| Text | A cell split by `;`, `\|`, `/`, `&` or a comma and a space into 2+ parts that each have a letter and 2+ characters (`R&D`, `AT&T`, `N/A`, `24/7` are not lists) | **Accept**: it is one value. **New Value**: the one value to keep (the first is named in the item). **Split**: one row per value |
| Numeric | Two or more numbers in one cell (`300;400`, `1,200 , 1,500`) | **New Value**: the single correct number |
| Numeric, ambiguous comma | `12,34` (decimal or two numbers) and `100,200` (thousands or two numbers) when no other cell uses that comma pattern; 3 or more cells with it make it the column's own convention and nothing is flagged | **Accept** reads it as the proposed number (`12.34`, `100200`); **New Value**: the right number |
| Date | Several dates in one cell | **New Value**: the one date |

* A column where more than half of the cells are lists (`multi_value_max_share`) gets one column item (a list field), not one per cell. At most
  `multi_value_max_cells` cells (200) are listed per column; the total is reported and the rest are held too.
* While an item is open the cell stays as written, and a numeric column that holds one is written as text in the output (nothing is
  converted silently); once the item is answered the column is typed again.
* **Split** (text items only, one column per row at a time) replaces the row by one row per value. Row_IDs become `<id>:s1`, `<id>:s2`; the
  Excel row stays the same. **The row's other columns are copied to every new row, so a number on it is repeated** (the Auto_Fix_Log says so):
  check totals before using split rows. The upload is rejected if Split is chosen on a number, date or column-level item.
* Settings (`hygiene:` in `settings.yaml` or `load_config` overrides): `multi_value_separators`, `multi_value_min_part`, `multi_value_max_share`,
  `multi_value_max_cells`, `multi_value_convention_cells`. There is no on/off switch (it never changes data).
* Not remembered across a fresh run: a cell that was answered is flagged again if the same file is run again.

## D-30, D-31, D-32 (added 2026-10-29)

**D-30 city names** (`rules.city_names`, matching in `src/hygiene/cities.py`; design in `docs/city_name_matching_design.md`). A column whose
name has `city`, `cities` or `town` is checked on its distinct values only: a hash of known names (the project's own list first, then
`config/city_names.yaml`, then an optional gazetteer), a delete-neighbourhood index for names within 1 or 2 edits, and a comparison of unknown
values with each other. One review item per spelling (`scope: value`, with the number of cells); **Accept** takes the proposed standard name and
**New Value** a name you type, for every cell with that spelling. Nothing is changed until answered and the cells are held back from later rules.
With a project city list (run inputs `cities`) its spellings are the standard and every other value is flagged. `Delhi NCR` is its own city.

**D-31 dates in order** (`rules.date_order`). Within each series (the label columns) dates should run one way. One column item says how many series
are out of order and names the first place; a table sorted newest first gets an info item. Nothing is sorted (the transformation step sorts).
Phase C repeats the check as N-13 for the tables it checks; D-31 covers the tables Phase C skips.

**D-32 expected columns** (`rules.expected_columns`). The run inputs list the columns the project expects in every sheet (`all_sheets`) and per
sheet; a missing one is flagged with the nearest column name. `strict: true` also notes columns that are not listed. A column chosen in
`columns_to_check` but absent from the sheet is an info item.

**Run inputs** (`src/common/run_inputs.py`, `config/run_inputs.yaml`, `GET` / `PUT /api/v1/runs/{id}/inputs`): expected columns, which columns to check,
the city list, and the default currency with conversion rates (quarterly or daily). The default file is used until a run saves its own copy at
`<run>/config/run_inputs.yaml`. Phase B restricts its rules to the chosen columns (the others are passed on as read; a column that would need
cleaning is written as text, not typed). Currency and rates are stored and validated; `rate_for` looks a rate up. No module converts values yet.

