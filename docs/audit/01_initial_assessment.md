# Initial assessment (Phases 1–4): discovery, audit, architecture, plan

Date: 2026-11-11. Branch audited: `docs/agent-architecture` at `b357191`.
Every finding below was either reproduced by running code or read directly in the file named. Tool output is quoted
where it is the evidence.

## 1. What the project does

A **Data Quality (DQ) service** for client spreadsheets. An analyst uploads Excel/CSV files; the service checks them in
phases, a person reviews each phase, and the cleaned tables come out as csv / parquet / xlsx with an audit trail.

| Phase | Package | What it does |
|---|---|---|
| A · Structure | `src/structure` (6,600 lines) | Finds tables, header rows and footers inside messy workbooks |
| B · Hygiene | `src/hygiene` (3,900) | 33 cell rules: dates, types, spellings, units, duplicates, negatives, multi-value cells |
| C · Time series | `src/timeseries` (1,700) | Missing periods, repeats, cadence breaks |
| D · Trends | `src/trends` (1,900) | Spikes, level shifts, monthly-to-weekly |
| T · Transform | `src/agent/transform` (1,400) | Optional reshaping (unpivot, split, allocate, aggregate…) from a recipe |
| Fix-with-agent | `src/agent` (3,900) | Plain-English instructions → typed operations → preview → approve → apply → undo |

Size: 120 Python files / 22,407 lines in `src`, 45 test files / 9,774 lines, 33 frontend files / 4,813 lines.

## 2. Existing architecture

```
Browser (React SPA, polls every 1–6 s)
   │  HTTP  /api/v1/*                         Platform gateway ── POST /jobs (Job Contract v1)
   ▼                                                    │
FastAPI app (one process, src/api) ─────────────────────┘
   │  calls
   ▼
Pipeline (src/orchestration/orchestrator.py)  state machine: locked→ready→running→review→accepted
   │  owns state.json per run (atomic replace, in-process RLock)
   │  JobRunner: in-memory job table, max 2 jobs, one OS process per job
   ▼
Worker process (src/orchestration/worker.py) ── runs ONE phase action, writes result.json, exits
   │                                   └── Agent actions (message / apply / undo) run here too
   ▼
Phase packages (structure, hygiene, timeseries, trends, agent/transform)  ── Ollama on 127.0.0.1 (optional)
   ▼
Run folder on local disk: inputs/, phase outputs (parquet + json), uploads/, jobs/, agent/, final/
```

**Execution flow.** Upload → `create_run` → `start(A)` launches a worker → worker writes outputs → watcher thread sets
the phase to `review` → analyst downloads the review workbook, edits and re-uploads it, or uses Fix-with-agent →
`accept` unlocks the next phase → after D (and optional T) the final files are built.

**External dependencies.** Ollama (local, optional), LibreOffice (optional, for `.xls/.xlsb/.ods`). No database, no
cloud service, no telemetry.

## 3. What is implemented well (keep)

- **Phase adapters** (`src/orchestration/phases/base.py`): each phase sits behind one interface.
- **Process isolation per job**: timeout, cancel, crash containment (`runner.py`, `worker.py`).
- **One apply path**: a chat change and a workbook upload go through the same hygiene code.
- **Agent safety model**: typed pydantic operations, schema-constrained model output, preview before apply,
  stale-proposal check, undo. Formula evaluation uses an AST allow-list, not `eval` (`executors.py:196`).
- **Upload validation** (`src/security/uploads.py`), hardened XML parsing and zip-bomb limits (`structure/xlsx.py:27-30,87`).
- **LLM is loopback-only by default** (`structure/llm/client.py:63`), model digest recorded.
- **Tests**: 679 backend + 78 frontend, all passing before this audit.

## 4. Problems found

Severity: **High** = data loss or security breach possible; **Medium** = production blocker; **Low** = quality debt.

| # | Sev | Where | Problem and consequence | Fix |
|---|---|---|---|---|
| F-01 | **High** | `src/structure/job.py` `_inputs`, `run_job` | **Path traversal in the Job Contract.** `root / item["ref"]` and `root / body["output_prefix"]` accept `..` and absolute paths. *Reproduced:* a job read a file outside the storage root and created `written_outside/` outside it. Any caller of `POST /jobs` can read and write files anywhere the service user can | Resolve every ref and confine it to the storage root |
| F-02 | **High** | `orchestrator.py` `start()` | **Results destroyed when the job limit is hit.** `_clear_from` deletes the phase's outputs *before* `runner.launch`; if launch raises `Busy`, the state is not saved. *Reproduced:* restart refused with `busy`, phase still `review`, artifacts 4 → 0 | Reserve the job slot first; delete only once launch is certain |
| F-03 | Medium | `agent/selectors.py:123`, `agent/executors.py:156,308` | **Regex denial of service.** Patterns from the analyst or the model go to Python `re` with no limit. *Reproduced:* `(a+)+$` on a 33-character cell ran past 5 s. An apply job could hold a worker for the full 180-minute job timeout | Time-limited matching, pattern length limit |
| F-04 | Medium | `api_server.py` `/jobs` | Body is an unvalidated `dict` (missing key → 500); the in-memory job table grows forever; job token is posted to the callback | Typed request model, bounded table |
| F-05 | Medium | `api_server.py`, `v1.py` | No security headers, no request id, no catch-all error handler, unstructured logs. Unexpected errors return a bare 500 with no trace id | Middleware + JSON logging |
| F-06 | Medium | `security/auth.py` | Failed sign-ins only sleep 1 s per request; parallel guessing is not limited | Per-client lockout |
| F-07 | Medium | `requirements.txt` | **No pinned versions, no lock file**; `pytest` listed as a runtime dependency. Builds are not reproducible; an upstream release can break production | `pyproject.toml` + locked requirements |
| F-08 | Medium | `src/agent` | **No evaluation set** (accuracy unknown); no conversation memory; no audit of model calls; one operation is defined in five places (model, executor, parser, LLM schema, prompt text) so they can drift; no repair attempt when the model's answer is invalid | Tool registry, eval harness, memory, call log |
| F-09 | Medium | `orchestrator.py`, `run_store.py` | State is one `state.json` per run under an in-process lock; the job table is in memory. **Only one service instance can run**; there is no audit store; `list_runs` reads every run's file | Keep for single-node (documented limit); add an audit database now; PostgreSQL is a later, separate migration |
| F-10 | Medium | repo root | No Dockerfile, no Compose, no reverse-proxy config, no `.env.example`, no graceful drain (shutdown kills running jobs) | Phase 8 |
| F-11 | Medium | whole `src` | `mypy` reports **260 errors in 60 files**; there is no `pyproject.toml` and no type gate | Gate the infrastructure packages; report the rest honestly |
| F-12 | Low | `frontend/package.json` | `npm audit`: 5 vulnerabilities (2 critical) in **dev-only** tools (vitest, esbuild, tinypool). Production bundle: 0 | Upgrade dev dependencies |
| F-13 | Low | `structure/extract.py`, `hygiene/review.py`, `hygiene/rules.py` | Very complex functions: `build_table` CC 65, `_select_columns` 59, `apply_upload` 40, `precheck` 38. Hard to change safely | Not rewritten in this pass (high regression risk, well tested); listed as debt |
| F-14 | Low | `structure/pipeline.py:34` | `STRUCTURE_HASH_KEY` falls back to a fixed development secret | Refuse the fallback when `DQ_ENV=production` |
| F-15 | Low | `config/settings.yaml` | Phase A defaults to `llama3.2:3b` reviewing **every** decision, although the project's own test showed it overruling the rules wrongly 14/14 times | Default to `near_ties`; decide by benchmark |
| F-16 | Low | repo | README is still half template; two Word lock files (`~$…docx`) are committed; `prompts/` and `docs/governance` are template leftovers | Rewrite README; remove lock files; keep the template folders unless told otherwise |
| F-17 | Low | `agent/workspace.py` | Undo copies the whole phase folder per applied change (up to 10 copies) | Documented; change with dataset versions later |

Scan results behind this table: `ruff` security rules 13 hits (all reviewed: 2 real — F-01 is not detectable by lint;
the `urlopen` hits are loopback-guarded; SHA-1 at `common/values.py:84` is a non-security fingerprint), `bandit` 4
medium+ (same items), `vulture` 0 dead-code hits at ≥ 80% confidence, `pip-audit` no known vulnerabilities in the
currently installed versions.

## 5. Proposed architecture

**A modular monolith on one server, hardened — not microservices.** The phases share one run, one state machine and
one dataset; splitting them would add distributed failures for no benefit. The realistic production target for a
privacy-first tool is one machine (or VM) the company controls.

```
                       ┌──────────────── one host, Docker Compose ────────────────┐
Browser ── HTTPS ──▶ Reverse proxy (Caddy): TLS, body limits, rate limit, headers  │
                       │                                                            │
                       ▼                                                            │
                 DQ app container (FastAPI + workers as child processes)            │
                 ├─ api/        HTTP only: validate, authenticate, shape answers    │
                 ├─ orchestration/  state machine, job runner, phase adapters       │
                 ├─ agent/      registry → planner → guardrails → preview → apply   │
                 ├─ domain: structure / hygiene / timeseries / trends / common      │
                 ├─ storage/    run folders (data) + audit.db (SQLite, WAL)         │
                 └─ security/, observability/                                       │
                       │ loopback / compose-internal network only                   │
                       ▼                                                            │
                 Ollama container (no published port, no internet egress)           │
                       │                                                            │
                 Volumes: runs (data), audit.db, recipes, ollama models             │
                       └────────────────────────────────────────────────────────────┘
```

**Dependency rule (one direction):** `api → orchestration → agent / phase packages → common`;
`storage`, `security`, `observability` are leaf utilities; nothing imports `api`.

**Decisions and why**

| Decision | Why | Rejected alternative |
|---|---|---|
| Stay a modular monolith | One team, one dataset, strict phase order | Microservice per phase: network hops and data copies, no gain |
| Keep run folders + `state.json` for run data | Works, atomic, self-contained, heavily tested; replacing it now is a rewrite with regression risk | PostgreSQL + object storage now: right for multi-node, wrong as a first step |
| Add **SQLite (WAL) audit database** | The service has no audit store; SQLite needs no server, is safe across the worker processes on one host, and migrates to PostgreSQL later | Log files only: not queryable, not tamper-evident |
| Agent = **typed-operation planner behind a tool registry** | Small local models are reliable at one schema-constrained choice, unreliable at long tool loops; every change stays previewable and auditable | Free tool-calling loop or code generation |
| No MCP in the core | Chat, tools and data are in one process; MCP adds nothing there. The registry exports MCP-shaped tool definitions so an adapter is cheap later | MCP-first |
| Local inference only, enforced in code | Privacy requirement; non-loopback model URLs are refused unless explicitly allowed | Cloud model (documented as an opt-in with its privacy cost, never default) |
| Shared access token now, identity seam for SSO | Real accounts need an identity provider decision (question 2) | Building OIDC against an unknown provider |

## 6. Technology choices

Python 3.12+, FastAPI, Polars/PyArrow, pydantic v2, React 18 + Vite (all already in use and current). Added:
`regex` (time-limited matching), SQLite from the standard library, Caddy as reverse proxy (automatic TLS, small
config), Docker Compose. Model serving stays Ollama (structured output through its `format` JSON-schema parameter).

## 7. Execution plan (in order)

1. **P0 defects**: F-01, F-02, F-03, each with a regression test.
2. **Service hardening**: F-04, F-05, F-06, F-14, audit database, graceful shutdown.
3. **Agent engineering**: tool registry, memory, repair retry, guardrails, call audit, evaluation harness with a
   non-agentic baseline.
4. **Testing and gates**: type gate on infrastructure packages, security scans in CI, frontend dev-dependency upgrade.
5. **Production engineering**: pinned dependencies, Dockerfile, Compose, proxy, environment separation, CI.
6. **Documentation and final audit** with an evidence-based readiness rating.

## 8. Questions (work proceeds on the stated default)

1. **Deployment target** — one server with Docker Compose (default), or Kubernetes?
2. **Sign-in** — which identity provider (Azure AD / Google / Okta / Keycloak)? Default: keep the shared token; add the seam.
3. **Hardware** — is a GPU (24 GB) available? It decides the agent model (7B–14B vs 3B on CPU).
4. **Template leftovers** — may `prompts/`, `TEMPLATE_CHANGELOG.md`, `docs/governance/` be removed? Default: keep.
5. **Multi-server / PostgreSQL** — needed in the first release? Default: no; single node.
