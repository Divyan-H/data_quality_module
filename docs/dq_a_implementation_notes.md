# DP-A implementation notes (`src/structure/`)

Spec: [dq_a_structural_intake.md](architecture/specs/dq_a_structural_intake.md). Tests: `pytest` and `pytest -m perf`.

## Run it

| What | How |
|---|---|
| Local run on files or folders | `python -m src.structure.cli <files\|folders> -o out [-c config.json]` |
| Job Contract v1 (job body, events) | `src.structure.job.run_job(body, storage_root, emit)`; `HttpEmitter` posts to `callback_url` |
| HTTP service | `src.api.api_server.create_app(storage_root)`: `POST /jobs` (idempotent on job id and attempt), `POST /jobs/{id}/cancel`, `POST /review/precheck` (raw workbook body), `GET /health` |
| LLM benchmark (spec 10.9) | `python -m src.structure.llm.benchmark cases.jsonl --model <name> --digest <sha256:...>` |

Environment: `STRUCTURE_LLM_URL` (default `http://localhost:11434`, loopback only unless `STRUCTURE_LLM_ALLOW_REMOTE=1`),
`STRUCTURE_HASH_KEY` (secret for question hashes; a fixed development value is used when unset),
`STRUCTURE_STORAGE_ROOT` (service storage directory).

## Where each part of the spec lives

Section numbers are those of spec v0.3. Code comments still cite v0.2 numbers in places (e.g. "spec 10.6" means
v0.3 §5.6); treat the module column as authoritative.

| Spec (v0.3) | Module |
|---|---|
| §3.1 format sniffing, file codes | `sniff.py`, `pipeline.process_file` |
| §4.1 streaming OOXML reader, canonical rendering, 1900/1904 dates | `xlsx.py`, `classify.py`, `colors.py` |
| §4.2 CSV, Parquet footer; §3.1 LibreOffice conversion | `readers.py` |
| §4.3 region finder, Excel Tables, pivots, XY-cut, keys `t`/`a`/`o` | `regions.py`, `extract._apply_overrides` |
| §4.4–4.6 header scorer, header block, names, footers, interior totals | `header.py`, `extract.py` |
| §4.7, §8.6, §8.7 fixer steps, overrides, cell edits | `extract.py` |
| §4.8 invariants (only I-01, I-05, I-06 checked — see gaps) | `extract._check_invariants`, `build_table` |
| §5 LLM review: views, votes, checks, decision record, corrections, budget, fallback | `llm/`, `decisions.py` |
| §6 findings S-01..S-28 and named codes, action items | `findings.py`, `features.py`, `facts.py`, `pipeline._finalize` |
| §7 `Row_ID`, §9 outputs | `writer.py`, `pipeline.py` |
| §8.5 selection, layout groups, S-09 families | `selection.py`, `features.families` |
| §10 review workbook, pre-check, import | `review_workbook.py`, `review_import.py` |
| §11 job contract, reuse, cancellation | `job.py`, `pipeline.py` |

## The review workbook: what was found, fixed and flagged

`Structural_Review.xlsx` carries two read-only sheets beyond the spec's list (`review_issues.py`):

- **Issues**: one row per finding, sorted Warning, then Fixed, then Identified. *Warning* = severity warn or
  block, needs a look; *Fixed* = Phase A changed the table to resolve it; *Identified* = reported, data left as
  stored. Each row names the file, sheet, table and cells, and says what was done.
- **Changes**: one row per structural change from each file's `audit.parquet`: promoted headers, peeled titles,
  attached groups, dropped columns, removed gap rows, footers set aside, merges filled, splits, overrides and
  cell edits.

Next to them, the table sheets (`T1 …`) hold the fixed tables and `Excluded_Cells` holds everything moved out.
The import ignores Issues and Changes.

## Gaps closed in the review of 2026-09-30

Sheet JSON now follows §9.4. Invariants I-02, I-03 and I-04 are checked. A `Total` row under a date column is
a footer. S-22 uses the Excel Table's column names. S-10 has the `merge_numeric` annotation and counts by
class. S-21 reads `firstHeaderRow` and has the grand-total heuristic. S-26 reports external-link parts. S-13
notes subtotals that close outlined groups. A failing sheet keeps its cells as `unread` placements. The audit
records `peel_title` and `split_table`. LLM re-cut rounds are capped at 3. Files run in parallel (rules only).
CSV is decoded as a stream. `override_orphaned` at config level keeps its `warn`. `Structural_Review.xlsx` is
byte-identical across runs.

Two bugs found and fixed on the way: a file with no table at all crashed the job, and the review workbook's
bytes depended on the clock (openpyxl stamps save time into the zip and `docProps/core.xml`).

## Break test of 2026-10-02

Two workbooks in `samples/` cover every structural issue: `structural_test_A.xlsx` (one issue per sheet,
modelled on the spec's golden workbook) and `structural_test_B.xlsx` (edge cases, saved by real Excel with a
real pivot, Excel Table, AutoFilter, external link and threaded comment). Rebuild them with
`python tests/fixtures/build_structural_workbooks.py` then `tests/fixtures/excel_post.ps1` (needs Excel);
`samples/external_budget.xlsx` is the external link's target. 42 expectations in `test_structure_workbooks.py`.

31 hostile inputs (truncated XML, entity bomb, junk bytes, 2 MB CSV field, cell at XFD1048576, 3,000 merges,
1,500 columns, 300 sheets, 1 M cells in 45 s, real Excel `.xls`/`.xlsb`/`.ods`/encrypted files) caused no crash.

Defects found and fixed (each has a test in `test_structure_breakage.py` or `test_structure_workbooks.py`):
- Merged ranges were ignored when cutting regions (spec §4.3): a group label merged across an empty column
  let the table split, and the hidden column beyond it became a one-column table.
- A spacer column with no cell inside a table vanished instead of being listed as dropped (S-05).
- A peeled title's merge was missing from the S-10 counts.
- Encrypted `.xls` (FILEPASS) and encrypted `.ods` were reported `file_unreadable`, not `password_protected`.
- A grid of whitespace-only cells became a table.
- Every run without a pinned model reported `llm_fallback`; a CSV's file name triggered S-24.
- **Review workbook:** cell types and comments bled between sheets (every sheet's first table is `t1`), so Excel
  had to repair the file; opening and saving it in Excel created phantom edits (Excel keeps 15 significant
  digits). Both fixed; an untouched Excel round trip now imports as a no-op, and real Excel edits apply.

## Live LLM test of 2026-10-02 (`llama3.2:3b`, RTX 2050 4 GB)

| File | Questions / calls | Time | Agreed / changed / overruled | Tables vs rules-only |
|---|---|---|---|---|
| structural_test_B | 26 / 78 | 346 s | 23 / 1 / 2 | identical |
| structural_test_A | 24 / 72 | 327 s | 10 / 0 / 14 | identical |
| test_data | 3 / 9 | 57 s | 2 / 0 / 0 (1 no majority) | identical |

Every answer parsed; no fallback. Every answer against the rules was wrong, and the checks refused it: the model
picked the group row (*Identifiers/Metrics*) as header, chose a header row on an all-number sheet, and voted to
split `Aesthetic_Gaps` at each of its 12 blank rows. The one `changed` decision (Two_Row_Header: row 1 instead of
group row 1 + header row 2) gives the same column names. Cost: the two extra `uncertain_header` warnings that
§5.6 requires on an overruled majority, both on tables the rules got right with margins of 3.5 and 0.5.

- `review: near_ties` asked 51 calls instead of 150 for A+B (232 s vs 673 s) and skips the wide-margin questions
  that produced the noise.
- At `num_ctx` 16,384 the model runs 49% on CPU on a 4 GB GPU (4.4 GB loaded); at 4,096 it is 80% on GPU (2.9 GB)
  and calls are about 21% faster. Real prompts here are ≤ 4,737 chars (~1,500 tokens), but the spec's view budget
  (§5.5) allows prompts up to 12,000 tokens, so the context stays at 16,384 until the spec changes.
- Fixed: with no digest pinned, decisions recorded `model_digest: null`; the digest the server reports is now recorded.

## Rule-pack extensions (not in the spec)

- **Footers under dates.** §4.6 footer test 2 counts a column as typed when ≥ 80% of its body is **numbers or
  dates** (spec: numbers only), so `Total` in a date column breaks the column pattern. Without it, a `Total` row
  whose sums are formulas without stored results was kept as a data row.
- **Key-value preamble.** A block of at most 6 `label | value` rows directly above a table (`Client | Acme`,
  `Period | Q1`) is a `key_value` placement, not a table. The spec's own key-value rule needs every header score
  below the minimum, which a block made only of labels never has; with auto-selection such blocks would reach
  Phase B as two-row tables.
- **Whitespace is not a table.** A region whose cells are all whitespace is a fragment.

## Spec contradiction resolved in code

§4.5 says a group row has "fewer *distinct* labels than" the header row, but §15.1's fixture (*Q1*/*Q2* over
*Budget, Actual, Budget, Actual*) has 2 distinct labels in each, so the rule would reject it. The code compares
populated-cell counts instead (2 < 4). Needs a spec decision.

## Design choices worth knowing

- **Findings come from facts.** Every table's value-free facts are written to `files/{key}/file.json`; findings are
  built from those, so a reused file gives the same findings without being read again.
- **Reader.** `lxml.iterparse` over each worksheet, elements cleared as they are read. openpyxl was rejected: it keeps
  the whole workbook in memory, and it converts dates itself, which cannot follow the spec's rendering rules
  (serial 60, out-of-range serials, the 1904 system). A 150,000-cell workbook runs end to end in about 7 s.
- **Rules own coordinates.** The LLM only picks among the rules' options, votes are counted over requested votes,
  and a change against the rules needs a unanimous vote the shape allows (`decisions.py`).

## Deviations and gaps still open

- **Memory (P8) is not bounded.** Each sheet's cells are held in memory while it is processed (XLSX and CSV);
  CSV is decoded as a stream, but its cells are still kept. Bounding memory needs a streaming extraction pass
  (spec §4.1 pass 3). Large sheets are capped by `sheet_too_large` instead.
- **One LLM decision per call** (the spec allows up to 5). Kept on purpose: a 3B model answers one question more
  reliably than a batch. Cuts, classes, headers and footers are asked in that order per sheet.
- **Parallel files only without the LLM.** With the LLM on, files run one after another: the spec's single
  ordered question queue would otherwise need a two-phase design.
- **Cancellation** is honoured between files and sheets, not between 50,000-row batches.
- **A side calculation next to a table becomes one of its columns** when no empty column separates them (per
  §4.3). Exclude it with Exclude columns in Structural_Findings.
- The model digest is read from `/api/tags` (Ollama's `/api/show` has no digest field).
- XLS, XLSB and ODS need LibreOffice (`soffice`) on the PATH: that path was not exercised here (not installed).
  An encrypted legacy XLS (`FILEPASS`) is not recognised; encrypted OOXML packages are.
- No pivot fixture exercises `firstHeaderRow`; the code follows OOXML `CT_Location` (0-based offsets).
- New-table rows in `Structural_Findings` also allow editing **File** (the spec locks it), since a blank row must
  name its file.
- Platform changes (gateway, PostgreSQL, object storage, staleness) are outside this repository; storage is a
  local directory and `job.py` is the seam.
- The golden workbook `data_quality_structural_issues_5000_rows.xlsx` is not in the repository. The tests build
  analogues of its traps; `test_structure_perf.py` covers its size target.
- Soft cuts: a far side only justifies a cut when its header row is all label cells (stops a data row scoring
  4.5 from being read as a header).
