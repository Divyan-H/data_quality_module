# Porting guide: bring every change from this repository into the production repository

**Read this whole file before changing anything.**

## 0. Instructions for Claude

You are working in the **production repository** of the Data Pipeline (DQ) service. The user has put a copy of a
second repository next to it: `data_quality_module`, branch `reshape-in-phase-a`, downloaded as a zip. That copy
holds about three weeks of changes made in one Claude Code cloud session (2026-11-02 to 2026-11-19). Your job is to
carry **all** of those changes into the production repository, so production behaves exactly like the copy.

How to work:

1. **The copy is the source of truth** for everything listed in this guide: the agent, transformations (reshaping),
   security, operations, deployment, tests and documentation. Port all of it. Do not redesign, simplify, rename or
   skip parts. If something looks odd, the reason is usually written in the code comments, in
   `docs/decisions/decision_logs.md` or in `docs/audit/`.
2. **Production may have its own changes** made after 2026-11-02 that the copy does not have. Keep them. When both
   sides changed the same code, merge them so that both behaviours survive. If you cannot tell which behaviour is
   wanted, especially for a business rule in Phases A–D, **stop and ask the user**. Do not guess.
3. **Port in the order of section 3** (each stage builds on the one before it). Run the tests after each stage
   (section 6) and fix failures before moving on.
4. **Do not copy** the items in section 7 (local data, build output, environment files).
5. When finished, report to the user, stage by stage:
   - what was ported;
   - what conflicted and how you resolved it;
   - which tests pass, with the actual numbers;
   - anything you could not port, and why.

### Your inputs

| File in the copy | What it is |
|---|---|
| `porting/PORTING_GUIDE.md` | This guide |
| `porting/changes.patch` | **Every text change since the base commit**, as one unified diff (1.2 MB). Binary documents (`.docx`, `.xlsx`, `.png`) are left out |
| `porting/FILES_CHANGED.txt` | Every file added (A), modified (M), deleted (D) or renamed (R) since the base |
| the rest of the copy | The finished result: when in doubt, the file in the copy is the target state |

**Base commit:** `dbb23604172ef82c03e9ed1a5ae4d6b551ae52eb`, "adding project files", 2026-11-02 (the original project
files). Every change below is relative to it.

## 1. Choose the porting method

First check whether the production repository still contains the base version of the files:

```bash
git apply --check porting/changes.patch
```

Run this from the production repository root, after copying the `porting/` folder in.

| Result | What to do |
|---|---|
| No output (it applies cleanly) | Production has not changed these files since the base. Run `git apply porting/changes.patch`. Copy the files that are not in the patch from the copy (`porting/FILES_CHANGED.txt`, binary documents only, optional). Then go straight to section 6 |
| Errors on some files | Run `git apply --3way porting/changes.patch` if the production history contains the base commit; otherwise `git apply --reject porting/changes.patch` and port each `.rej` hunk by hand, keeping production's own changes (rule 2) |
| Production is very different | Port feature by feature following section 3, using the copy's files as the reference and the patch to see exactly what changed |

Whatever the method, every item in sections 3–5 must be present at the end. Use them as your checklist.

## 2. What the service looked like at the base, and what it is now

**Base:**
- Phases A (structure), B (hygiene), C (time series) and D (trends), each followed by a human review in Excel.
- FastAPI service with a React web app; one worker process per job; run state in a `state.json` per run.
- No agent, no access control, unpinned dependencies, no Docker.

**Now:**
- **Pipeline:** A → B → C → D. Phase A is now "detect the tables, then optionally reshape them with a recipe", and
  Phases B–D check the reshaped tables.
- **Agent, two chats:**
  - *Reshape with agent* on Phase A's review;
  - *Fix with agent* on Phase B's review.
  Each turns a plain-English instruction into **typed operations** from a fixed list, shows a preview, and changes
  data only when a person clicks Apply. Every change can be undone and is audited.
- **Local model only:** the model runs through Ollama on this machine or an allow-listed host; no data goes to a
  public AI service.
- **Security:** optional access token with a sign-in screen and lockout, security headers, an audit database,
  metrics.
- **Packaging:** pinned dependencies, Docker / Compose with a reverse proxy, CI with lint, type and security gates.
- **Tests:** 739 backend and 80 frontend tests, all passing at the copy's last commit.

## 3. The changes, stage by stage (port in this order)

Each stage lists what it does and the files involved. Exact line changes are in `porting/changes.patch`.
"(new)" = a file that did not exist at the base.

### Stage 1 — Agent design documents
- `docs/agent_architecture.md` (new): the design of the agent (typed operations, planner, preview, approval).
- `docs/agent_build_plan.md`: was already present.

### Stage 2 — Fix-with-agent for Phase B (the core agent)
What it does: in Phase B's review the analyst types an instruction such as `accept all date fixes except row 14`,
`Head Office is Bangalore`, `Spend is in thousands` or `add column Revenue = Orders * AOV`.
1. A **deterministic command parser** reads it first.
2. If the parser cannot, **one schema-constrained call** to the local model plans it.
3. The plan becomes **typed operations**, which are dry-run into a **preview**.
4. **Apply** runs them through the same code as a review-workbook upload; **Undo** restores a snapshot.

Files:
- **Package `src/agent/` (new):**

  | Part | Files |
  |---|---|
  | Typed operations | `operations.py` |
  | Row and column selection | `selectors.py` |
  | Execution | `executors.py` |
  | Edit tracking | `session.py` |
  | Phase B data | `workspace.py`, `views.py` |
  | Change-sets and chat | `store.py` |
  | Use cases | `service.py` |
  | Plain-language text | `describe.py` |
  | Errors | `errors.py` |
  | Planner | `planner/__init__.py`, `planner/parser.py`, `planner/llm.py`, `planner/context.py` |

- **Wiring:**
  - `src/orchestration/agent.py` (new): when the chat may act, and the agent jobs;
  - edits to `src/orchestration/orchestrator.py`, `src/orchestration/worker.py`, `src/orchestration/phases/base.py`
    and `src/orchestration/phases/hygiene.py` (`supports_agent`, `agent_workspace`, `agent_views`);
  - `src/api/v1.py`: agent endpoints under `/api/v1/runs/{id}/phases/{key}/agent/...`.
- **Hygiene changes:**
  - `src/hygiene/config.py`: an `agent` rule text;
  - `src/hygiene/review.py`: the re-check after an apply skips D-32 (expected columns), which judges the whole sheet.
- **Frontend:**
  - `frontend/src/components/agent/` (new): `AgentWorkspace.tsx`, `ChatPanel.tsx`, `IssueList.tsx`, `SheetView.tsx`,
    `logic.ts`;
  - edits to `api.ts`, `types.ts`, `hooks.ts`, `panels.tsx`, `RunPage.tsx`, `styles.css`.
- **Tests:** `tests/test_agent_core.py`, `tests/test_agent_api.py`, `frontend/src/__tests__/agent.test.tsx` (new).
- **Docs:** `docs/agent_implementation_notes.md` (new).

### Stage 3 — Transformations and the new operations
What it does: replaces the hand-run Porter scripts with a **recipe of typed steps**: unpivot, pivot, split rows,
allocate by share, aggregate, bucket others, net next period, append rows, lookup join, convert currency, sort, make
date, make period, drop columns, drop table, plus split column and classify. It was first built as an optional
Phase T after D; Stage 12 moves it into Phase A.

Files:
- `src/agent/transform/` (new): `engine.py` (every operation on Polars frames, with row lineage),
  `workspace.py` (recipe, replay, preview, apply, undo, report), `describe.py`.
- `src/agent/planner/transform_commands.py` (new): plain-English wording for these operations.
- `src/orchestration/sources.py` (new): which file holds each final table.
- `src/orchestration/bundle.py`: uses it.
- Frontend: `frontend/src/components/agent/RecipePanel.tsx` (new); edits to `summaries.tsx` and `panels.tsx`.
- Tests: `tests/test_transform_engine.py` (each Porter script's worked example), `tests/test_transform_commands.py`,
  `tests/test_transform_api.py` (new).
- Docs: `docs/transformations.md` (new).

### Stage 4 — First review round: bugs, access token, operations guards, Phase B recipes, CI
- **Bug fixes:**
  - the model's JSON schema had a duplicate `rules` key;
  - `classify` may create a new column when planned by the model;
  - `scale` on mixed text/number columns produced untidy text;
  - loop-variable closures in `src/hygiene/rules.py` (`parse_all`, `root`).
- **Access token:** `src/security/auth.py` (new).
  - Shared token from `DQ_API_TOKEN` (16+ characters): `Authorization: Bearer` for systems, or an HttpOnly
    SameSite=Strict session cookie for the browser.
  - The service refuses to listen beyond 127.0.0.1 without a token.
  - Frontend: `frontend/src/components/SignIn.tsx`, edits to `App.tsx` and `api.ts`, and
    `frontend/src/__tests__/signin.test.tsx` (new).
- **Job Contract:** `callback_url` must be on the `STRUCTURE_CALLBACK_HOSTS` allow-list; `/review/precheck` streams
  the upload with a 200 MB cap (`src/api/api_server.py`).
- **Operations guards:** `GET /api/v1/ready`, a disk guard (`507 disk_full` below `min_free_disk_mb`), and an
  optional retention purge (`retention_days`), in `src/orchestration/orchestrator.py`, `settings.py` and
  `config/settings.yaml`.
- **Phase B recipes:** `src/agent/recipes.py` (new). `save recipe as X` / `use recipe X`; steps that name Excel rows
  are left out.
- **Tooling:**
  - the agent's rows view reads only the requested page (`src/agent/views.py`);
  - `ruff.toml`, `requirements-dev.txt` and `.github/workflows/ci.yml` (new);
  - tests `tests/test_security_auth.py` and `tests/test_operations.py` (new).

### Stage 5 — Start scripts
`scripts/start.sh` and `scripts/start.ps1` rebuild the web app whenever the frontend sources are newer than the
build, not only when it is missing.

### Stage 6 — Three audited defects (high priority even if nothing else is ported)
1. **Path traversal in the Job Contract** (`src/structure/job.py`): every `inputs[].ref`, `output_prefix` and
   `cell_edits_ref` is resolved and must stay inside the storage root (`inside()`).
2. **Results lost on a refused restart** (`src/orchestration/runner.py` and `orchestrator.py`): the job slot is
   reserved before a re-run discards outputs (`launch(..., before_start=...)`, `LaunchFailed`).
3. **Regular-expression denial of service** (`src/agent/safe_regex.py` (new); used in `selectors.py`,
   `executors.py` and `transform/engine.py`): patterns from users or the model are limited to 500 characters and
   0.25 s per match, using the `regex` library.

Also: the Word lock files `~$*.docx` are deleted and added to `.gitignore`. Tests: `tests/test_regressions_audit.py`
(new). Assessment: `docs/audit/01_initial_assessment.md` (new).

### Stage 7 — Service hardening
- **Middleware and logging:** `src/observability/` (new).
  - `http.py`: request id, access log, security headers (CSP, `X-Frame-Options: DENY`, `nosniff`, `no-referrer`,
    HSTS over HTTPS, `Cache-Control: no-store` on API answers), and a JSON 500 that carries the request id;
  - `logs.py`: JSON logs (`DQ_LOG_FORMAT`, `DQ_LOG_LEVEL`);
  - `metrics.py`: Prometheus counters;
  - `context.py`: the request-id context variable.
- **Audit database:** `src/storage/audit.py` (new). SQLite in WAL mode with versioned migrations
  (`PRAGMA user_version`), append-only, with tables `audit_events` and `llm_calls`. Values, prompts and tokens are
  never stored.
- **Sign-in lockout:** `src/security/auth.py`. Five failures in five minutes from one address → 429.
- **API** (`src/api/api_server.py`):
  - typed `JobBody` for `POST /jobs`, a job table bounded at 500, and one error shape `{"error": {...}}` for every
    refusal;
  - `/docs` and `/redoc` switched off (their CDN scripts break the CSP);
  - production start-up checks.
- **API** (`src/api/v1.py`): `/api/v1/metrics`, `/api/v1/audit` and `/api/v1/audit/llm`.
- **Orchestration:** `orchestrator.py` (audited actions via the `audited` decorator, job metrics, retention audit),
  `runner.py` (`drain`, `DQ_SHUTDOWN_GRACE_S`), and `worker.py` (model calls recorded in the audit database).
- **Settings** (`settings.py`): `DQ_ENV` (development | test | staging | production), `audit_db`,
  `shutdown_grace_s`, lockout settings, and `production_problems()` (refuse to start without a token and a hash key).
- **Tests:** `tests/test_hardening.py` (new).

### Stage 8 — Agent engineering
- **Tool registry** (`src/agent/registry.py` (new)): one `Tool` per operation, with phases, risk, row filter and
  summary. The model's prompt, its JSON schema, the phase limits and an MCP-shaped export (`mcp_tools()`) are all
  derived from it.
- **Guardrails** (`src/agent/guardrails.py` (new)): a change that removes rows, columns or tables, aggregates, or
  changes ≥ 1,000 cells is *elevated*.
  - Apply then needs `{"confirm": true}`; without it the API answers `409 confirmation_required`.
  - The UI shows the reasons and a tick box (`ApplyRow` in `ChatPanel.tsx`; `confirm` sent from `AgentWorkspace.tsx`
    and `api.ts`).
- **Planner** (`planner/llm.py`, prompt version `agent-plan/3`):
  - the model sees the last chat turns as data (memory);
  - one repair attempt when its answer is invalid;
  - at most 6 operations per plan;
  - phase limits enforced at planning time (`planner/__init__.py::_in_phase`).
- **Service** (`src/agent/service.py`): audit events `agent.planned`, `agent.applied` and `agent.undone`; the model
  and prompt version are stored on model-made change-sets.
- **Evaluation:** `src/agent/evaluation.py` and `evals/agent/cases.yaml` (new). 110 cases scored correct / safe /
  wrong; the parser-only baseline is recorded in `evals/agent/baseline_parser.json`.
- **Fix:** `rename column X to Y in <table>` used to put "in <table>" into the new name (`planner/parser.py`).
- **Tests:** `tests/test_agent_registry.py` and `tests/test_agent_eval.py` (new).

### Stage 9 — Type gate, a Phase C/D bug, frontend tools
- `mypy` must be clean on `src/api`, `src/orchestration`, `src/storage`, `src/security` and `src/observability`
  (small type fixes across those packages; see the patch).
- **Bug:** in `src/orchestration/phases/timeseries.py`, Phases C and D passed the file *description* where the
  *phase* belongs. The Results page showed a sentence as the phase, and zip paths and the primary download were
  wrong. Regression test in `tests/test_api.py` (`test_every_file_names_its_phase…`).
- Frontend dev tools upgraded: vite 8.3.8, vitest 5.1.0, @vitejs/plugin-react 6.3.0, jsdom 31.0.0
  (`frontend/package.json` and `package-lock.json`; npm audit goes from 5 issues to 0).

### Stage 10 — Production engineering
- **Pinned dependencies:**
  - `requirements.txt` and `requirements-dev.txt` hold exact versions with hashes;
  - their sources are `requirements/runtime.in` and `requirements/dev.in` (new);
  - regenerate with `uv pip compile --generate-hashes --universal --python-version 3.12 …`;
  - **new runtime dependency `regex`**; `pytest` and `httpx` moved out of the runtime set.
- **Packaging** (all new):

  | File | What it is |
  |---|---|
  | `pyproject.toml` | mypy and bandit configuration |
  | `Dockerfile` | Multi-stage; non-root uid 10001; LibreOffice optional (`WITH_LIBREOFFICE`) |
  | `.dockerignore` | Keeps local data and build output out of the image |
  | `docker-compose.yml` | Caddy → app (read-only, no capabilities, `init: true`) → Ollama on an internal network with no route out; a `model-pull` helper profile |
  | `deploy/Caddyfile` | TLS, body limits, `/metrics` hidden |
  | `.env.example` | The settings an operator fills in |

- **Model host allow-list:** `DQ_LLM_ALLOWED_HOSTS` (`src/structure/llm/client.py::host_allowed`,
  `src/llm_interface/ollama_status.py`). Prompts go only to this machine or a named host.
- **Audit SQL:** made constant, with bound parameters only.
- **SHA-1:** the finding-id hash is marked `usedforsecurity=False` (`src/common/values.py`).
- **CI** (`.github/workflows/ci.yml`): installs with hashes; runs ruff, the mypy gate, bandit, pip-audit, the tests,
  the evaluation report, npm audit and the frontend build; then builds the image and smoke-tests it.

### Stage 11 — Documentation set
- **New:** `docs/ARCHITECTURE.md`, `API.md`, `DATABASE.md`, `SECURITY.md`, `DEPLOYMENT.md`, `DEVELOPMENT.md`,
  `TESTING.md`, `ENVIRONMENT_VARIABLES.md`, `PROJECT_STRUCTURE.md`, `AI_AGENT.md`; `CHANGELOG.md`;
  `docs/audit/02_final_assessment.md`; `docs/audit/REVIEW_CHECKLIST.md`.
- **Rewritten:** `README.md`.
- **Moved:** `TEMPLATE_CHANGELOG.md` to `docs/governance/`.

### Stage 12 — Transformations move into Phase A (pipeline becomes A → B → C → D)
What it does: the transformation recipe is built with **Reshape with agent** on Phase A's review, and Phases B–D
check the reshaped tables. Phase T no longer exists.

Files:
- `src/orchestration/phases/reshape.py` (new): inputs, replay, summary, artifacts, the blocking rule, and the
  `phase_a_shaped/` output folder. The recipe is stored in `config/A_recipe.json`; an old `T_recipe.json` is taken
  over.
- `src/orchestration/phases/transform.py` is **deleted**; `ORDER = ["A", "B", "C", "D"]` in `phases/__init__.py`.
- `src/orchestration/phases/structure.py`: `supports_agent = True`, `replay()`, `agent_workspace()`,
  `agent_views()`, `reshape` in the summary, and `can_continue` blocked while a recipe step fails.
- `src/orchestration/orchestrator.py` and `worker.py`:
  - a `reshape` job action;
  - the recipe replays after a Phase A run, a review upload or a table-selection change;
  - old runs lose their `T` entry on start-up;
  - `is_complete` means D is accepted.
- `src/agent/transform/workspace.py::_save`: writes a `handoff.parquet` per changed table (row ids
  `<table ref>:r<n>`) and a `lineage.parquet`.
- `src/hygiene/io.py::shaped_tables` and `load_tables(..., shaped=)`, `src/hygiene/pipeline.py::run_hygiene(...,
  shaped=)`, and `src/orchestration/phases/hygiene.py`: Phase B loads untouched tables from Phase A as before, and
  changed, new or dropped tables from the recipe.
- `src/agent/guardrails.py`: reshaping steps that change numbers (aggregate, allocate by share, net next period,
  convert currency, bucket others) need confirmation, with the reason "Phases B–D will check the result".
- Wording that pointed to "Phase T" now points to "Reshape with agent" on Phase A's review, in `executors.py`,
  `planner/__init__.py`, `parser.py` (HELP), `planner/llm.py` and `transform/engine.py`.
- **Fix:** `classify` no longer adds its column to tables that lack the columns its rules test.
- **Frontend:** four phases (`types.ts`: `PhaseKey` is A–D); Phase A's agent card reads "Reshape with agent"; the
  recipe summary shows in Phase A's results (`panels.tsx`, `summaries.tsx`, `AgentWorkspace.tsx`, `ChatPanel.tsx`).
- **Tests:** `tests/test_transform_api.py` rewritten. It includes identical B–D findings with and without the
  handoff, and the migration of an old fifth phase. Other tests were updated for four phases.
- **Docs:** `docs/transformations.md` rewritten; ARCHITECTURE, AI_AGENT, API and DATABASE updated; decision log;
  CHANGELOG 1.2.0.

### Stage 13 — Review pass (latest)
- **Type checking:** `src/agent` is type-clean and added to the CI mypy gate.
  - `Operation` is now an explicit union.
  - New `src/agent/tables.py` holds `PlanTable`, the table shape both workspaces satisfy.
  - The parser's `column()` always returns a column.
  - `TransformCommands` declares the interface it expects from its host class.
- **Fixes:**
  - a single value in an `in` / `not_in` condition was matched character by character (and a number raised); it is
    now read as a list of one (`operations.value_list`, used in `selectors.py` and `transform/engine.py`);
  - a selection change that breaks the recipe now puts an accepted Phase A back in review and locks Phase B;
  - `bucket others` with nothing to keep answers with a clear message.
- **Design:**
  - without a recipe, nothing is written for reshaping (no copy of every table), and a selection change answers at
    once;
  - the Reshape screen pages straight from Phase A's files (`TransformViews(out, inputs)`, `reshape.views()`);
  - the chat uses `StructurePhase.tables()` (cheap) instead of `summary()`.
- **Docs:** `IMPROVEMENTS.md` (new, the prioritised list of what is still open); CHANGELOG 1.2.1.

## 4. Behaviour and API changes the production system will see

| Change | Detail |
|---|---|
| Pipeline | A → B → C → D. `/api/v1/runs/{id}/phases/T/...` answers `404 unknown_phase`; reshaping is at `/phases/A/agent/...` |
| Apply of an elevated change | `POST …/agent/changesets/{cs}/apply` needs `{"confirm": true}`, else `409 confirmation_required` with `reasons` |
| Errors | Always `{"error": {"code", "message", ...}}`, including validation errors (was FastAPI's `{"detail": ...}`) |
| `POST /jobs` | Validated (422 on a malformed body); `job_token` required with `callback_url`; refs confined to the storage root |
| Docs pages | `/docs` and `/redoc` off; `/openapi.json` stays |
| Production start | `DQ_ENV=production` refuses to start without `DQ_API_TOKEN` and `STRUCTURE_HASH_KEY` |
| Non-loopback host | Refused without an access token unless `DQ_ALLOW_UNAUTHENTICATED=1` |
| New endpoints | `/api/v1/ready`, `/api/v1/metrics`, `/api/v1/audit`, `/api/v1/audit/llm`, `/api/v1/session` (GET/POST/DELETE), every `/agent/...` endpoint |
| New files on disk | `audit.db` beside the runs folder; per run: `agent/<phase>/`, `phase_a_shaped/`, `config/A_recipe.json`; `recipes/` beside the runs folder |

## 5. Configuration and environment variables

New keys in `config/settings.yaml` under `service:`: `auth_token`, `retention_days`, `min_free_disk_mb`,
`agent: {llm, model}`, `env`, `audit_db`, `shutdown_grace_s`. Compare the copy's `config/settings.yaml` with
production's and add the new keys **without** overwriting production's own values.

New environment variables:

| Variable | Purpose |
|---|---|
| `DQ_API_TOKEN` | Access token |
| `DQ_ENV` | `development`, `test`, `staging` or `production` |
| `DQ_AUDIT_DB` | Audit database file |
| `DQ_RETENTION_DAYS` | Delete runs untouched for this many days |
| `DQ_MAX_CONCURRENT_JOBS` | Worker processes at once |
| `DQ_SHUTDOWN_GRACE_S` | Let running jobs finish this long on shutdown |
| `DQ_TRUSTED_PROXIES` | Proxies whose `X-Forwarded-*` headers are trusted |
| `DQ_CORS_ORIGINS` | Origins allowed cross-origin |
| `DQ_LOG_LEVEL`, `DQ_LOG_FORMAT` | Log level; `json` or `text` |
| `DQ_LLM_ALLOWED_HOSTS` | Hosts other than this machine that prompts may go to |
| `DQ_AGENT_MODEL` | Model for the agent |
| `DQ_ACK_REMOTE_LLM` | Confirms a remote model in production |
| `DQ_ALLOW_UNAUTHENTICATED` | Allows a network address without a token (development only) |

Full descriptions are in `docs/ENVIRONMENT_VARIABLES.md`. `STRUCTURE_CALLBACK_HOSTS` existed before but is now
enforced.

## 6. Verify (after each stage, and fully at the end)

Install the pinned dependencies (Python 3.12+):

```bash
pip install --require-hashes -r requirements-dev.txt
```

Then run the checks:

```bash
ruff check src tests
```

```bash
mypy src/api src/orchestration src/storage src/security src/observability src/agent
```

```bash
bandit -q -c pyproject.toml -r src -ll
```

```bash
python -m pytest -q -n auto
```

```bash
python -m src.agent.evaluation --show failures
```

```bash
cd frontend && npm ci && npm run typecheck && npm test && npm run build
```

Expected results at the copy's last commit:

| Check | Expected |
|---|---|
| ruff | clean |
| mypy (the six packages above) | clean |
| bandit | no findings |
| pytest | 739 passed |
| Evaluation, parser only | command tier 98.2%, adversarial tier 100%, **0 wrong** |
| Frontend | 80 tests pass; typecheck and build succeed |

Production's numbers will differ if production has its own tests. **No test may fail, and the evaluation may show no
wrong change.**

Then check by hand in the app:

1. Upload a workbook and run Phase A.
2. Click **Reshape with agent** and type `sort by <a column>`; Apply.
3. Accept A, run B, then try **Fix with agent** with `add column Source with value Client`.
4. Try `remove rows 3`: the Apply button must stay disabled until the confirmation box is ticked.

## 7. Do not copy

- `data/` (runs, the audit database, recipes): local data.
- `frontend/node_modules/` and `frontend/dist/`: build output, rebuilt from the sources.
- `.env`: secrets; only `.env.example` belongs in the repository.
- `dq_agent_branch.bundle`, `out_*`, `real_data_test/results/`, `.pytest_cache`, `.ruff_cache`, `.mypy_cache` and
  `__pycache__`.
- The `porting/` folder itself, once the port is done (keep it in a branch if you want the record).

Optional, only if the user wants them: the Word documents under `docs/` and `docs/reports/`, and `docs/audit/`
(the assessments describe this copy's state, so they need re-checking against production before they are trusted).

## 8. Decisions already taken (do not reopen them)

| Decision | Detail |
|---|---|
| One service, not microservices | Run state stays in files; PostgreSQL is future work (`IMPROVEMENTS.md` 2.1) |
| Local models only | No cloud model unless the user decides otherwise explicitly |
| No MCP in the core | `registry.mcp_tools()` is the seam for an adapter later |
| Reshaping lives in Phase A | The product owner chose this on 2026-11-16, accepting that number-changing steps run before the checks; it is mitigated by the confirmation and `lineage.parquet` |
| Phase A's model review stays at `all` | The owner's choice, although flagged in `IMPROVEMENTS.md` 4.8 |
| Template folders kept | `prompts/`, `docs/governance/` and `docs/readmes/` |

## 9. Known gaps (ported as they are, not fixed)

`IMPROVEMENTS.md` lists them in priority order. The three that block wider production:
1. The local model's accuracy has never been measured.
2. There are no user accounts.
3. There is no separation between clients.

Do not try to fix these during the port; port first, then raise them with the user.
