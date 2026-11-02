# Analyst web app and service layer (`src/api`, `src/orchestration`, `frontend/`)

Runs Phases A to D as one guided flow: upload, then for each phase *run → download → (edit) → upload → accept*, then
download the final files. The phases themselves are unchanged; this layer calls them (see the per-phase notes
[A](dq_a_implementation_notes.md), [B](dq_b_implementation_notes.md), [C](dq_c_implementation_notes.md),
[D](dq_d_implementation_notes.md)).

## Run it

```
ollama serve                       # own terminal; the app tells you if it is not reachable
scripts\start.ps1                  # builds the frontend once, then serves http://127.0.0.1:8000
```

Development: `python -m src.api` (port 8000) and `cd frontend && npm run dev` (port 5173, proxies `/api`).
Tests: `python -m pytest` (backend, starts real worker processes, about 5 min) and `cd frontend && npm test`.
Settings: the `service:` section of `config/settings.yaml` (every key is listed in `src/orchestration/settings.py`).
Environment: `DQ_WORKSPACE` (run folders), `DQ_SETTINGS`, `DQ_HOST`, `DQ_PORT`, `DQ_LLM_MODEL`, `STRUCTURE_LLM_URL`.

## The flow

| Screen | What happens |
|---|---|
| Home | Drop files or a folder (`.xlsx .xlsm .xls .xlsb .ods .csv .tsv .txt .parquet`, 500 MB total). Names and sizes are checked in the browser and again on the server. |
| Phase A | Runs `run_structure`. Live stage list, LLM call counter, log, Cancel. Result: tables found, issues (Warning / Fixed / Identified), changes made. Download `Structural_Review.xlsx`, edit, upload (a column typed after the last column of a table sheet is kept and added in Phase B); the upload is checked and applied (selection, overrides, cell edits) and the phase re-runs reusing everything unchanged. |
| Phase B | Runs `run_hygiene` on the tables A selected. 23 automatic fixes can be switched (D-15, a numeric column with negative values, is flag-only and has no switch; D-29, a cell holding more than one value, is flag-only too); D-14 and D-27 (fill an isolated zero) start off (see decisions). The start screen also asks in which columns zero is a valid value (never filled). A column that is the sum of other columns (a Total) is flagged (D-28, `buckets.json`). Download `Hygiene_Review.xlsx`, decide flagged items, upload; a new column typed after the last column of a `Data_<n>` sheet is added to the table and checked. |
| Phase C | Before it can start: download the **pre-filled client config**, fill the yellow cells, upload. The upload is checked against the data (`ConfigError` text is shown) and a preview says what will run. Several tables: the analyst picks one. Then `run_timeseries`. |
| Phase D | Same config step (separate workbook; thresholds, ratios). The preview lists which rules will run and which have no threshold. Then `run_trends`; monthly tables are also published as Monday-to-Sunday weekly tables (extra file, [notes](dq_d_implementation_notes.md)). |
| Not weekly data | Phase C needs a date column and a numeric measure and checks weekly series. `src/common/granularity.py` classifies each table (daily / weekly / monthly / quarterly / yearly / none, from the gaps between dates, a Year-Month label, or Year + Month columns). When Phase C or D does not apply, the config step says so ("the data is monthly, not weekly ... no numeric measure columns") and offers **Skip Phase C and continue**; the skip is recorded (`skipped`, `skip_reason`), the next phase reads the cleaned table of the phases before it, and the Results page lists only what really ran. *Set it up anyway* is always available. Phase D also needs Spend/Support pairs (Y rows) in Variable Mapping. |
| Results | Final table (`csv`, `parquet`, `xlsx` ≤ 100k rows), cleaned Phase B tables, the four review workbooks, audit JSON, configs used, `pipeline.log`, and one zip with a `MANIFEST.json` of SHA-256 checksums. |

After every phase the analyst can **Accept and continue** without edits. Uploading an edited workbook re-checks the phase and
discards later phases (their input changed). Restarting a phase asks for confirmation and discards it and everything after.

### The LLM rule

Only Phase A uses the local model (Ollama, `llama3.2:3b`); Phases B, C and D are fully deterministic. Before they start, the service probes Ollama (`/api/v1/llm/status`;
the top bar shows the state). If the model is unreachable or not installed, the start is refused with `409 llm_unavailable` and the UI
asks: **Check again / Continue without the LLM / Cancel**. Choosing to continue runs the rule-based logic and records
`llm.requested = "off"` on the phase, shown as *Rule-based fallback · LLM skipped*. What the model does in Phase A:

* **A**: reviews header, footer and table-cut decisions (`structure.llm`, default `review: all`, privacy `full`, 3 votes). It can
  only choose among the rules' options and the checks can overrule it. The UI counts the calls live.


## Architecture

```
browser ──HTTP──> src/api (FastAPI)  ──> src/orchestration.Pipeline  ──> JobRunner ──spawn──> worker process
  React UI          v1.py: validate,        state machine, locking,        one process per       src.orchestration.worker
  polls run +       shape answers           invalidation, LLM question     phase action          └─ phases/<A..D>.py adapters
  events                                    state.json (src/storage)                                 └─ src/structure | hygiene | timeseries | trends
```

* `src/api/v1.py` – the analyst API; thin. `src/api/api_server.py` – also keeps the Job Contract v1 endpoints (`/jobs`, `/review/precheck`).
* `src/orchestration/orchestrator.py` – owns `state.json`. Statuses: `locked → ready → running → review → accepted`, plus `failed`, `cancelled`.
* `src/orchestration/runner.py` – starts `python -m src.orchestration.worker <job_dir>`, watches it, kills the whole process tree on cancel,
  timeout (180 min) or run deletion; at most `max_concurrent_jobs` (2) at once, one per run.
* `src/orchestration/phases/*.py` – one adapter per phase behind one interface (`run`, `review`, `clear`, `summary`, `artifacts`,
  `candidates`, `make_template`, `check_config`). Adding Phase E/F/G = one adapter and one line in `phases/__init__.py`.
* `src/storage/run_store.py` – `data/runs/<run_id>/` (state, inputs, `phase_a/`, `work/` shared by B–D, `config/`, `uploads/`, `jobs/`, `final/`).
  A run folder is self-contained. Only the API process writes `state.json`; only the worker writes `events.jsonl`.
* Why a process per job: the phases configure global loggers (not thread-safe), Phase A uses its own process pool, a stuck or
  out-of-memory phase must not take the API down, and cancelling needs a process to kill.
* Progress: Phase A reports its stages; B reports tables cleaned; C and D report stages. The worker wraps `OllamaClient.chat` to count calls.
  Percentages are only shown where the phase gives real numbers; otherwise the bar is indeterminate. The UI polls every 1 s (no WebSocket).
* After a restart, runs left `running` are marked failed (`interrupted`) and any orphan worker is killed.

## API (`/api/v1`)

| | |
|---|---|
| `GET /health`, `/options`, `/llm/status?fresh=` | liveness; limits, fix catalogue; Ollama state |
| `POST /runs` (multipart `files[]`, `paths[]`, `name`, `llm`) · `GET /runs` · `GET/DELETE /runs/{id}` | create, list, view, delete |
| `GET /runs/{id}/events?after=N` | progress and log events after cursor N |
| `POST /runs/{id}/phases/{A-D}/start` `{llm?, llm_review?, fixes?, restart?}` | run a phase (409 `locked`, `restart_required`, `config_required`, `llm_unavailable`, `busy`) |
| `POST /runs/{id}/cancel` · `POST /runs/{id}/phases/{p}/accept` | stop the running job; accept results |
| `POST /runs/{id}/phases/{p}/review` (multipart `file`) | apply an edited review workbook (checked first; nothing changes if rejected) |
| `GET …/phases/B/columns` (numeric columns for "zero is a valid value"; start takes `zero_expected`) · `GET …/phases/{C,D}/tables` · `GET …/config-template?table=` · `POST …/config` · `POST …/config/recheck` | client config workbook step |
| `GET …/phases/{p}/summary` · `GET /runs/{id}/results` · `GET /runs/{id}/files/{id}` · `GET /runs/{id}/bundle` | results and downloads |

Errors are `{"error": {"code", "message", …}}` with a stable `code`.

## Security of this layer

Bound to `127.0.0.1` by default. **There is no login** (before_production P-02): do not expose the port. File names, folder paths, run
ids and download ids are validated (`src/security/uploads.py`); downloads only serve files from a fixed per-phase list; uploads are
streamed with a size cap; review workbooks must open as `.xlsx` before a phase sees them. Client data never leaves the machine; the LLM address
must be loopback unless `STRUCTURE_LLM_ALLOW_REMOTE=1`.

## Decisions made here (also in `docs/decisions/decision_logs.md`)

* **Phase B `D-14` (outlier capping) starts switched off.** On the planted weekly workbook, capping hid 9 of the 14 Phase D findings
  (5 found instead of 14) and rewrote genuine zero weeks. With it off all 14 are found (`tests/test_api.py`). Switchable per run.
* **LLM review depth defaults to `all`** (the spec default, and the analyst asked for the LLM to always be used). `near_ties` is one click
  away under Advanced and is much faster on messy workbooks.
* **Deselecting every table of a source now means "none selected"**, not "no selection configured" (which silently re-selected everything).
  Phase A raises a blocking `selection_not_found` and the app refuses to continue until a table is selected.

## Known gaps

* **Phases E (mapping), F (DuckDB load) and G (pre-load checks) are not built**; their specs exist. The flow ends at Phase D.
* No client report / Gate after Phase D (PDF); the `client_report` flags are in the findings files.
* Phase B's value-consolidation upload (D-26, PRD) and the optional data dictionary (D-16) have no UI; the dictionary is a settings key.
* `.xls`, `.xlsb`, `.ods` are converted by LibreOffice (found on the PATH, in Program Files, or in `%LOCALAPPDATA%\Programs\LibreOffice`). Where it is missing the home screen refuses those files and says to save them as `.xlsx`.
* No authentication, TLS or multi-user separation; no retention policy for `data/runs`; no cleanup job.
* A review upload for Phase A re-runs the whole phase (unchanged files are reused, but a 1M-cell file still takes minutes).
* Phase C lists every N-08 pair (a constant series of 260 weeks gives 33,670 rows); the UI previews the first rows of each code and the
  workbook holds up to `max_sheet_rows`. See [Phase C notes](dq_c_implementation_notes.md).
* Windows: two Python file handles appending to one file lose lines; events are therefore written with one OS-level append under a lock
  by the worker only (found by `tests/test_storage.py`).
