# Project structure

```
src/
  api/              HTTP only. api_server.py (app, Job Contract, middleware wiring), v1.py (analyst API)
  orchestration/    orchestrator.py (Pipeline state machine), runner.py (job processes), worker.py (a job),
                    agent.py (AgentController), settings.py, bundle.py (results zip), sources.py
    phases/         base.py (the Phase interface) + one adapter per phase
  agent/            registry.py, operations.py, planner/ (parser, llm, context), guardrails.py, executors.py,
                    selectors.py, safe_regex.py, service.py, store.py, workspace.py, views.py, recipes.py,
                    evaluation.py, describe.py, session.py
    transform/      engine.py (Polars), workspace.py, describe.py           — Phase T
  structure/        Phase A (readers, regions, header scoring, extraction, review workbook, llm/)
  hygiene/          Phase B (rules, values, cities, review, pipeline)
  timeseries/       Phase C          trends/   Phase D
  common/           shared by C and D (periods, dataset loading, roll-ups, run inputs)
  storage/          run_store.py (run folders, state.json), audit.py (SQLite audit database)
  security/         auth.py (token, session, lockout), uploads.py (name and id validation)
  observability/    http.py (middleware), logs.py (JSON logs), metrics.py, context.py (request id)
  llm_interface/    ollama_status.py (is the model usable?)
frontend/           React + Vite SPA (src/pages, src/components, src/__tests__)
tests/              pytest suite (see docs/TESTING.md)
evals/agent/        evaluation cases and the recorded parser baseline
config/             settings.yaml, run_inputs.yaml, city lists
deploy/             Caddyfile
requirements/       *.in sources of the pinned requirements*.txt
scripts/            start.sh, start.ps1, md_to_docx.py
docs/               this documentation, specs (architecture/specs), audit (audit/), implementation notes
samples/            example workbooks
```

## Dependency rules

1. `api` may import `orchestration`, `security`, `observability`, `storage`. Nothing imports `api`.
2. `orchestration` may import the phase packages and `agent`; they must not import `orchestration`.
3. `agent` may import `hygiene` (Phase B tables and rules) and `structure.llm.client`; not `orchestration` or `api`.
4. `timeseries` and `trends` share code through `common` only.
5. `storage`, `security`, `observability` import nothing from the layers above them (`storage.audit` uses
   `observability.context` for the request id).

Known exception: `common/values.py` imports the date parser from `hygiene/values.py`; moving that parser into
`common` is listed debt.

## Template leftovers

`prompts/`, `docs/governance/` (now holding `TEMPLATE_CHANGELOG.md`) and `docs/readmes/` come from the company project template.
They are not used by the service and were kept pending a decision.
