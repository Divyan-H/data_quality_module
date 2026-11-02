# Data Pipeline service: open issues, improvements, and running it in a bigger system

Status of the service as of 2026-10-13. Phases A to D and the analyst web app are built and tested (416 backend tests, 53 frontend
tests, end-to-end acceptance tests). This document is the honest list of what is still wrong, what should get better, and what has
to change before the service runs on a server or as one part of a larger architecture.

Priorities: **P0** = fix before anyone else uses it, **P1** = fix before production, **P2** = worth doing.

---

## 1. Issues to address

### 1.1 Bugs and defects known today

| # | Pri | Issue | Evidence / cause | Fix |
|---|---|---|---|---|
| B1 | P0 | **`ValueError: format %y requires year >= 1900` crashes Phases C and D** | `strftime("%B '%y")` is used in `src/timeseries/autoconfig.py:238`, `src/timeseries/checks.py:137`, `src/trends/autoconfig.py:234`. The shared `Periods` reader (`src/common/values.py`) enforces the 1900–2100 year range for text dates only; real date cells (e.g. Excel's `1899-12-30`) and Excel serial `1` pass through unchecked. | One `month_label(d)` helper without `strftime`; apply the year range to every kind of period value; serials start at 2. Add a test with a 1899 cell and a serial `1`. |
| B2 | P1 | Phase C reports tens of thousands of repeated-value findings (N-08 is pairwise: a constant series of n weeks gives n(n-1)/2 rows) | 26k–38k findings on the planted sample; 317 MB `findings.json` at 400 variables x 260 weeks (Phase C notes). | Spec decision: one row per (variable, value) listing its weeks. Until then the UI previews a few rows per code. |
| B3 | P1 | Phase C/D config steps are slow on large tables (table list 20–25 s, template 45 s) | The profile reads and classifies every cell. | Profile a sample of rows; compute once when Phase B is accepted, not when the screen opens. (The overlap error and repeat calls are already fixed.) |
| B4 | P1 | Re-uploading a Phase A review re-runs the whole phase | Unchanged files are reused, but a large workbook still takes minutes. | Per-table re-extraction instead of per-file. |
| B5 | P2 | A phase that crashes during a review upload is shown as *failed* although its old outputs may be fine | `_finished` in `orchestrator.py` sets `FAILED` on a `crash` outcome of a review job. | Keep old outputs in a backup folder during the upload and restore them on failure. |
| B6 | P2 | The Phase A directory is swapped (wipe, then rename) during a review upload; for a moment the folder does not exist | `StructurePhase.review`. The API tolerates it (files that vanish are left out of that answer) but a download at that instant returns 404. | Rename the old folder aside, rename the new one in, then delete the old. |
| B7 | P2 | `Hygiene_Review.xlsx` has one Needs_Review row per flagged cell | A noisy column gives thousands of rows. | Group by column and rule with a "apply to all" decision. |

### 1.2 Specification gaps (documented requirements that are not built)

| # | Pri | Gap | Where specified |
|---|---|---|---|
| G1 | P0 for the PRD end goal | **Phases E (mapping), F (DuckDB load) and G (pre-load checks) do not exist.** The flow ends at Phase D. | `docs/architecture/specs/dq_e_mapping.md`, `dq_f_loading.md`, `dq_g_db_checks.md`; PRD Phases E–G |
| G2 | P1 | No client report / Gate after Phase D (PDF); only the `client_report` flags in the findings files | PRD "Gate after Phase D"; before_production section 3 |
| G3 | P1 | Phase B D-26 value-consolidation upload (group raw category values into one bucket) | PRD Phase B, user story 3 |
| G4 | P2 | Phase B D-16 data dictionary has no UI (settings key only) | `src/hygiene/config.py` |
| G5 | P2 | Phase A memory is not bounded: a sheet's cells are held in memory; huge sheets stop with `sheet_too_large` | Phase A notes, P8 |
| G6 | P2 | Success metrics are blank in the PRD, so "done" cannot be measured | PRD "Success Metrics" |
| G7 | P2 | Phase A spec contradiction (§4.5 "distinct labels" vs the golden fixture) needs a spec decision | Phase A notes |

### 1.3 Quality of the LLM use (Phase A only)

* The 3B model overruled the rules 14 times out of 14 in the earlier live test and was wrong each time; the safety checks refused it.
  It currently adds cost (minutes on a small GPU with `review: all`) but, on this evidence, little accuracy. Measure before relying on it:
  run the Phase A benchmark (`src/structure/llm/benchmark.py`) on real files with `qwen2.5:7b-instruct` and compare against the rules alone.
* `review: all` is the default (spec default, requested). `near_ties` is about 3x faster. Decide the default from measured accuracy.
* The model, digest and prompt version are recorded per decision, but there is no regression suite that fails when a model change makes answers worse.

### 1.4 Security (the service has no login)

| # | Pri | Issue | Fix |
|---|---|---|---|
| S1 | P0 for any shared server | **No authentication or authorization** on any endpoint (before_production P-02). The service is bound to `127.0.0.1` to compensate. | Validate a gateway-issued JWT (or mTLS) on every request; map the caller to a tenant and project. |
| S2 | P0 for any shared server | **No tenant separation.** Every run is visible to every caller (`GET /api/v1/runs` lists all). | Add `tenant_id` and `owner` to run state; filter and check on every access. |
| S3 | P1 | No TLS in the service itself | Terminate TLS at the reverse proxy; require it for the callback URLs too. |
| S4 | P1 | Job Contract `callback_url` is called with the job token without an allow-list (server-side request forgery risk) | Allow-list callback hosts. |
| S5 | P1 | Uploaded Office files are parsed in the API host's worker processes; LibreOffice runs unsandboxed with the user's rights | Run workers and LibreOffice in a locked-down container, no network (before_production L-08, L-09). |
| S6 | P1 | No rate limiting, no per-user quota, no antivirus scan on uploads | Gateway rate limits; quota per tenant; scan at the edge. |
| S7 | P1 | Client data stays on disk forever; no retention or delete-on-request job | Retention setting, purge job, audit of deletions (before_production L-10, L-11). |
| S8 | P1 | `STRUCTURE_HASH_KEY` falls back to a fixed development value | Fail to start in production without a real secret from a secrets manager (L-16). |
| S9 | P2 | Logs are value-free by rule for Phase A; Phases B–D `pipeline.log` and the review workbooks contain cell values | Decide the rule for B–D; keep logs value-free and put values only in the run folder. |

### 1.5 Reliability and operations

| # | Pri | Issue | Fix |
|---|---|---|---|
| R1 | P1 | **State and jobs live in one process on one disk**: `state.json` per run, an in-memory job table, worker processes on the same host. A second server instance would not know about the first's jobs. | Move state to a database (PostgreSQL) and jobs to a queue; keep one service instance until then. |
| R2 | P1 | No structured logging, metrics, tracing or alerts. `src/monitoring/monitoring.py` is still a template stub. | JSON logs with run and job ids; Prometheus metrics (jobs by phase and outcome, durations, LLM calls, fallbacks); alert on failed-job rate and `llm_fallback`. |
| R3 | P1 | No graceful drain: stopping the service kills running workers | On SIGTERM stop accepting jobs, let running ones finish (bounded), then exit. |
| R4 | P1 | `data/runs` grows without bound; no disk-space check before a run | Quota and cleanup; refuse uploads when the disk is nearly full. |
| R5 | P1 | Jobs are not retried and not queued: the 3rd concurrent job gets `429 busy` | A queue with positions shown in the UI; retry transient failures. |
| R6 | P2 | The browser polls every 1 s (run state and events) | Server-sent events for events and state changes. |
| R7 | P2 | The sync config steps hold an HTTP request open for up to 180 s | Make them asynchronous like the phases: return 202, poll the run. |
| R8 | P2 | Windows file-handle quirks (lost appended lines, replace while reading) were found and worked around | Keep the tests; plan the production target as Linux containers. |

### 1.6 Code and repository hygiene

* **Unused template stubs** (`ingestion/`, `processing/`, `analytics/`, `storage/save.py`, `utils/`, `monitoring/`,
  `llm_interface/llm_client.py`, `security.authorize`) were removed, with their placeholder tests.
* **Generated output committed to the working tree:** `out_*` folders (now git-ignored), `real_data_test/results`, `samples/*_issue_catalogue.xlsx`.
* **`real_data_test/` may contain client data.** It was deliberately left out of git; decide where real test data lives.
* Phase A code comments cite spec v0.2 section numbers; the notes say v0.3 numbers are authoritative. Renumber.
* About 20 s of every backend test run is process start-up; the suite takes about 8 minutes. See "Faster tests" below.
* No lint/format/type-check gate in CI (only tests, the frontend typecheck and build). Add `ruff` and `mypy` (or pyright) to `.github/workflows/ci.yml`.
* The front end has no browser-level test (Playwright). Folder drag-and-drop and real file downloads are untested in a browser.

---

## 2. Improvements worth making

**Product**
1. Phase E–G (mapping, load, checks) so the service ends with data in DuckDB as the PRD says.
2. Per-client saved configuration: remember the Phase C/D config and the Phase B fix switches per client, pre-select them on the next run (PRD open question 2).
3. A client report (PDF) from the `client_report` findings.
4. Run comparison: this month's run against last month's (new and resolved findings).
5. Review inside the app instead of Excel round trips (an editable grid for Needs_Review), keeping the workbook as the audit copy.
6. Comments and an audit trail per decision (who accepted what, when), needed once there is more than one user.

**Engineering**
1. Single dependency direction: `common` -> `hygiene` -> `timeseries`/`trends` -> `orchestration` -> `api`. `src/common/values.py` imports from `src/hygiene/values.py` (the date parser); move that parser into `common` so `common` depends on nothing.
2. Phase interface for E–G: the adapter in `src/orchestration/phases/` already isolates a phase behind eight methods; add one adapter per new phase.
3. Typed API models (Pydantic response models) so the OpenAPI document and the TypeScript types are generated from one source instead of written twice.
4. Generate the frontend client from `/openapi.json`.
5. Replace pairwise N-08 (B2) and per-cell Needs_Review rows (B7) to cut output size.
6. Faster tests: run with `pytest -n 4` (xdist), share one finished A to B run between read-only tests, mark the LibreOffice and 250k-row tests `slow`.

**Performance**
1. Profile on a row sample (B3). 2. Per-table Phase A re-extraction (B4). 3. Stream Phase A cell extraction to bound memory (G5). 4. Write review workbooks in write-only mode everywhere (Phase C already does).

---

## 3. The API when the service is deployed or is part of a larger system

### 3.1 What exists

The service has **two API surfaces on one FastAPI app**. Interactive documentation is generated at `/docs` and `/openapi.json`.

**A. Analyst API, `/api/v1`: used by the bundled web app**

| Method and path | Purpose |
|---|---|
| `GET /api/v1/health` | Liveness and number of active jobs |
| `GET /api/v1/options` | Limits, extensions, whether legacy files can be converted, the Phase B fix catalogue |
| `GET /api/v1/llm/status?fresh=` | Is Ollama reachable and the model installed |
| `POST /api/v1/runs` (multipart: `files[]`, `paths[]`, `name`, `llm`) | Create a run with its input files |
| `GET /api/v1/runs`, `GET/DELETE /api/v1/runs/{id}` | List, read, delete |
| `GET /api/v1/runs/{id}/events?after=N` | Progress and log events after cursor N |
| `POST /api/v1/runs/{id}/phases/{A-D}/start` | Start a phase (`llm`, `llm_review`, `fixes`, `restart`) |
| `POST /api/v1/runs/{id}/cancel` | Stop the running job |
| `POST /api/v1/runs/{id}/phases/{p}/accept` | Accept results and unlock the next phase |
| `POST /api/v1/runs/{id}/phases/{p}/review` (multipart `file`) | Apply an edited review workbook |
| `GET …/phases/{C,D}/tables`, `GET …/config-template`, `POST …/config`, `POST …/config/recheck` | Client config step |
| `GET …/phases/{p}/summary`, `GET /runs/{id}/results`, `GET /runs/{id}/files/{id}`, `GET /runs/{id}/bundle` | Results and downloads |

Errors are `{"error": {"code", "message", ...}}` with stable codes (`locked`, `restart_required`, `config_required`, `llm_unavailable`,
`busy`, `upload_rejected`, `too_large`, ...). Run ids are 16 hex characters; artifact ids come from a fixed per-phase list.

**B. Job Contract v1: for a platform gateway (Phase A only)**

| Method and path | Purpose |
|---|---|
| `POST /jobs` (202) | Submit a `data.structure` job; idempotent on `(job_id, attempt)`; events go to `callback_url` with a bearer `job_token` |
| `POST /jobs/{id}/cancel` | Cancel |
| `POST /review/precheck` (raw workbook body) | Synchronous check of a Phase A review workbook |
| `GET /health` | Liveness |

**Important limits of B today:** `job_type` must be `data.structure`; Phases B, C and D are **not** reachable through the Job Contract.
Job state is held in memory (an idempotent re-submit after a restart runs the job again); there is no authentication; storage is a local directory
(`STRUCTURE_STORAGE_ROOT`), not object-store references.

### 3.2 What has to change to run it on a server

| Concern | Today | For a server |
|---|---|---|
| Bind address | `127.0.0.1` (`api.host`) | Bind `0.0.0.0` only behind a reverse proxy on a private network. |
| Process model | `python -m src.api`, one uvicorn process, workers as child processes | Run under a supervisor or container; one instance until R1 is solved. |
| Packaging | None | Container image: Python 3.12 + `requirements.txt`, Node build stage for `frontend/dist`, LibreOffice in a separate sandboxed worker image. |
| Storage | Local disk `data/runs` | A persistent volume now; S3/MinIO with per-job scoped credentials later (before_production L-06). |
| LLM | `http://localhost:11434`, loopback only | An internal model server; set `STRUCTURE_LLM_URL` and `STRUCTURE_LLM_ALLOW_REMOTE=1`, and re-decide the privacy default (L-01, L-03). |
| Config | `config/settings.yaml` + env | Env vars per environment: `DQ_WORKSPACE`, `DQ_HOST`, `DQ_PORT`, `DQ_LLM_MODEL`, `STRUCTURE_LLM_URL`, `STRUCTURE_HASH_KEY` (required). |
| Health | `/api/v1/health`, `/health` | Add `/ready` (workspace writable, disk space, Ollama optional) for the orchestrator. |
| Reverse proxy | none | `client_max_body_size` at least 500 MB; read timeout at least 200 s (the sync config steps hold a request up to 180 s); no response buffering for downloads; pass `X-Forwarded-*`; the app serves the SPA at `/` and the API under `/api/`. |
| CORS | Allows the Vite dev origins | Same-origin behind the proxy; remove the dev origins from `service.cors_origins`. |
| Sessions | None needed | Because state is on one disk, route a run to one instance (sticky by run id) if more than one is ever started. |
| Backups | None | Back up the workspace volume; runs are self-contained folders. |

### 3.3 What has to change to be one part of a larger architecture

The platform diagram (Architecture v2.1, Specs 01–03) puts a **gateway** in front of workers, with PostgreSQL for state, object storage for files and an LLM
orchestration service. To fit, in this order:

1. **Authenticate and scope.** Accept the gateway's per-service JWT (`iss`, `aud`, 5-minute expiry) or mTLS; read `tenant_id`, `project_id`, `user` from it; store them on the run; reject cross-tenant access. (S1, S2)
2. **One contract for every phase.** Extend Job Contract v1 so `job_type` can be `data.hygiene`, `data.timeseries`, `data.trends` (and later `data.mapping`, `data.load`, `data.checks`), each with its inputs
   (`raw_file`, `config`, `previous_<phase>`, `review_upload`), `output_prefix`, outputs with content hashes, and the same events (`accepted`, `progress`, `log`, `succeeded`, `failed`).
   The adapters in `src/orchestration/phases/` already take exactly these inputs, so the work is in `src/structure/job.py`'s equivalent per phase and the routes.
3. **Object storage instead of a local disk.** Resolve input and output refs against S3/MinIO using the job's scoped credentials (Spec 02 6.3); stage inputs to local scratch, write outputs under `output_prefix`.
4. **Durable job state.** Keep run and job state in PostgreSQL; make `POST /jobs` idempotency survive restarts; run phase actions from a queue so any worker host can take a job; move events to the platform's event stream.
5. **Callbacks and events.** Keep `callback_url` with the job token, add an allow-list (S4), retry with back-off, and sign payloads.
6. **Split the deployment.** API/gateway-facing container, worker container(s) running `python -m src.orchestration.worker`, sandboxed LibreOffice, and the model server, each scaled on its own.
7. **Observability.** Propagate a request id from the gateway into every log line, event and worker; export metrics (R2).
8. **Versioning.** Keep `/api/v1` stable; add fields, never rename; announce breaking changes as `/api/v2`; publish the OpenAPI document as the contract and run a contract test against it in CI.
9. **Downstream hand-off.** Phase D's `tables/<ref>/data.parquet` (with `_row_id`) is the input of Phase E. Publish it as a registered dataset (path, schema, content hash, row count) so the next service reads it by reference rather than by folder layout.

### 3.4 A suggested call sequence for another system

1. `POST /api/v1/runs` with the files; keep the returned `id`.
2. `POST /runs/{id}/phases/A/start`; if the answer is `409 llm_unavailable`, decide: retry, or repeat with `{"llm": "off"}` (recorded as a fallback).
3. Poll `GET /runs/{id}` (state) and `GET /runs/{id}/events?after=<cursor>` until the phase is `review`.
4. `GET /runs/{id}/phases/A/summary`; optionally download the review workbook, edit, `POST …/review`.
5. `POST …/accept`; repeat for B.
6. For C and D: `GET …/config-template`, fill it, `POST …/config` (a `422` lists what to fix), then `…/start`.
7. After D is accepted: `GET /runs/{id}/results` and `GET /runs/{id}/bundle`.

---

## 4. Suggested order of work

1. **Now (days):** B1 (the `%y` crash), B3 (cheaper profiling), remove the template stubs, add lint/type checks to CI, `pytest -n 4`.
2. **Before any shared server:** S1–S4, S8, R4, container image, reverse-proxy settings, `/ready`, structured logs and metrics.
3. **To meet the PRD:** Phases E, F, G; the client report; D-26; per-client saved config.
4. **To fit the platform:** the contract for Phases B–D, object storage, PostgreSQL state and a queue, callbacks with allow-list and signing.
5. **Quality:** measure the LLM on real files (1.3), split N-08 (B2), group Needs_Review (B7), browser tests.
