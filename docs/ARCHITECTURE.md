# Architecture

A **modular monolith on one host**: one Python service that serves the web app and the API and runs each job in its
own worker process, a reverse proxy in front, and a local model server beside it. No cloud service is involved.

## Components

```
Browser ──HTTPS──▶ Caddy (TLS, body limits)                     Other systems ──▶ /jobs (Job Contract v1)
                     │                                                    │
                     ▼                                                    ▼
        ┌─────────────────────── DQ service (one process) ───────────────────────┐
        │ HttpHardening middleware: request id, access log, security headers     │
        │ Token guard + sign-in lockout (security/auth.py)                       │
        │ api/        thin HTTP layer: validate, call Pipeline, shape the answer │
        │ orchestration/                                                         │
        │   Pipeline      state machine per run; owns state.json                 │
        │   JobRunner     one worker process per job, limit, timeout, drain      │
        │   phases/       one adapter per phase (A, B, C, D, T)                  │
        │   AgentController  when the chat may act; what an apply means          │
        └───────────────┬────────────────────────────────────────────────────────┘
                        │ subprocess: python -m src.orchestration.worker <job dir>
                        ▼
        Worker process (one phase action, then exits)
          ├─ structure / hygiene / timeseries / trends      domain packages
          └─ agent/  registry → planner (parser, then local model) → guardrails → preview → apply
                        │ HTTP, allow-listed host only
                        ▼
        Ollama (loopback, or the `ollama` container on an internal network with no route out)

        Storage on the host: runs/<run id>/… (data, parquet, json)   audit.db (SQLite, WAL)   recipes/
```

## Layers and dependency rule

`api → orchestration → agent / phase packages → common`. `storage`, `security` and `observability` are leaf
utilities any layer may use. Nothing imports `api`; the domain packages do not import `orchestration`.

| Layer | Package | Responsibility |
|---|---|---|
| Presentation | `frontend/` | React SPA; polls run state; never holds business rules |
| API | `src/api` | Request validation (pydantic), uploads streamed to disk, one error shape |
| Application | `src/orchestration` | Run state machine, jobs, phase adapters, agent controller |
| Domain | `src/structure`, `hygiene`, `timeseries`, `trends`, `agent`, `common` | The checks, the typed operations, the transformation engine |
| Infrastructure | `src/storage`, `security`, `observability`, `llm_interface` | Run folders, audit database, auth, logs, metrics, model probe |

## Run lifecycle

```
locked ──(previous phase accepted)──▶ ready ──start──▶ running ──▶ review ──accept──▶ accepted
                                                          │           │  ▲
                                                          │           └──┘ review upload, agent apply, agent undo
                                                          └──▶ failed | cancelled   (start again)
```

A change in a phase (upload, apply, undo) discards the outputs of every later phase: they checked older data.
Only a person accepts a phase; the agent has no tool for it.

## Data flow of one instruction

1. `POST …/agent/messages` → a short worker job. The **command parser** reads common wording with no model;
   otherwise **one** schema-constrained call goes to the local model, with a single repair attempt.
2. The plan is checked against the **tool registry** (is the tool allowed in this phase?) and dry-run on the data.
3. **Guardrails** mark destructive or large changes as *elevated*.
4. The proposal (typed plan + preview) is stored. Nothing has changed.
5. `POST …/apply` (with `confirm: true` when elevated) → a worker job applies it through the same code a review
   upload uses, re-checks the changed cells, snapshots for undo, and records audit events.

## Design decisions

| Decision | Why | What it costs |
|---|---|---|
| Modular monolith, one host | One team, one dataset, strict phase order | One instance only (see below) |
| Process per job | Cancel, timeout, crash and memory isolation | ~0.4 s start-up per job |
| Files for run data, `state.json` per run | Self-contained, atomic, easy to back up and delete | No cross-run queries; single writer process |
| SQLite (WAL) for the audit trail | Queryable, safe across the worker processes, no server to run | Single host |
| Typed-operation planner | Reliable with small local models; every change previewable and auditable | New kinds of change need a new operation |
| Local model only, host allow-list | Client data stays on the host | Smaller models; hardware on the host |
| No MCP in the core | Chat, tools and data share a process | `registry.mcp_tools()` is the seam for an adapter |

## Known limits

- **One service instance.** The job table is in memory and `state.json` is guarded by an in-process lock. Running
  two instances on one workspace would corrupt state. Scaling out needs state and jobs in PostgreSQL first.
- **One shared identity.** The access token says "allowed", not "who". Audit rows record the action, not a person.
- **Undo copies a phase's output folder** per applied change (10 kept).
- Phases E–G (mapping, DuckDB load, pre-load checks) are specified in `docs/architecture/specs/` but not built.
