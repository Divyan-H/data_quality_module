# Development

## Setup

Python 3.12+, Node 22.

```bash
python -m venv .venv && source .venv/bin/activate
```

```bash
pip install --require-hashes -r requirements-dev.txt
```

```bash
cd frontend && npm ci
```

## Run

```bash
sh scripts/start.sh
```

Builds the web app when it is missing or stale, then serves everything on <http://127.0.0.1:8000>. For frontend
work with hot reload, run `python -m src.api` in one terminal and `npm run dev` in `frontend/` (port 5173, proxied).
`DQ_LOG_FORMAT=text` gives readable logs. The local model is optional: `ollama serve`, `ollama pull llama3.2:3b`.

## Checks before a commit

```bash
ruff check src tests
```

```bash
mypy src/api src/orchestration src/storage src/security src/observability
```

```bash
python -m pytest -q -n auto
```

```bash
cd frontend && npm run typecheck && npm test
```

## Changing dependencies

Edit `requirements/runtime.in` or `requirements/dev.in`, then regenerate both pinned files:

```bash
uv pip compile --generate-hashes --universal --python-version 3.12 requirements/runtime.in -o requirements.txt
```

```bash
uv pip compile --generate-hashes --universal --python-version 3.12 requirements/dev.in -o requirements-dev.txt
```

Run the full suite afterwards.

## Adding an agent operation

1. The input model in `src/agent/operations.py` (add it to `VALUE_TYPES` or `SHAPE_TYPES`).
2. The executor in `src/agent/executors.py` (Phase B) and/or `src/agent/transform/engine.py` (Phase T).
3. One `Tool` in `src/agent/registry.py`: phases, risk, summary. `tests/test_agent_registry.py` fails until the
   three agree.
4. Wording in `src/agent/planner/parser.py` or `transform_commands.py`, and a line in `describe.py`.
5. Cases in `evals/agent/cases.yaml` (documented wording and at least one paraphrase).

## Adding a phase

One adapter in `src/orchestration/phases/` implementing `Phase` (run, review, artifacts, summary, clear,
can_continue), and one entry in `phases/__init__.py`.

## Conventions

- Refusals the analyst can act on are exceptions with a `code` and a plain `message` (`PipelineError`,
  `AgentError`, `PhaseError`); they become the API's error shape.
- Log identifiers and counts, never cell values. Audit rows follow the same rule.
- A defect gets a regression test that fails on the old code (`tests/test_regressions_audit.py`).
