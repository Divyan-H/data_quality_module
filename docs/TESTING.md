# Testing

## Layers

| Layer | Where | What it proves |
|---|---|---|
| Unit and domain | `tests/test_hygiene*.py`, `test_structure_*.py`, `test_timeseries.py`, `test_trends.py`, `test_transform_engine.py` | Rules, extraction, checks and every transformation, including each Porter script's worked example |
| Agent | `test_agent_core.py`, `test_agent_registry.py`, `test_transform_commands.py`, `test_operations.py` | Parser, operations, preview/apply parity, undo, registry consistency, guardrails, memory, repair |
| Agent evaluation | `test_agent_eval.py`, `evals/agent/cases.yaml` | 110 instructions: no wrong change, must-not-change cases held, documented commands ≥ 95% |
| API and end to end | `test_api.py`, `test_agent_api.py`, `test_transform_api.py`, `test_acceptance.py`, `test_e2e_feature_check.py` | Whole flows through HTTP with real worker processes |
| Orchestration | `test_orchestration.py`, `test_storage.py` | Jobs: cancel, crash, timeout, limits, recovery after a restart |
| Security | `test_security.py`, `test_security_auth.py`, `test_hardening.py`, `test_regressions_audit.py` | Upload validation, token and session, lockout, headers, error shape, path confinement, regex limits, model-host allow-list, audit contents |
| Frontend | `frontend/src/__tests__` (vitest, jsdom) | Components, API client, sign-in, proposal cards, guardrail confirmation |

## Commands

```bash
python -m pytest -q -n auto            # 730 tests, about 2 minutes on 4 cores
```

```bash
python -m pytest -q -m perf            # performance targets of the Phase A spec (not in the default run)
```

```bash
cd frontend && npm test                # 80 tests
```

```bash
python -m src.agent.evaluation --llm on --model qwen2.5:7b-instruct      # needs Ollama
```

## Gates in CI (`.github/workflows/ci.yml`)

Dependencies installed with hashes · `ruff` · `mypy` on `src/api`, `src/orchestration`, `src/storage`,
`src/security`, `src/observability` · `bandit` (medium and above) · `pip-audit` · the full test suite · the
evaluation report as an artifact · `npm audit` (high and above) · frontend typecheck, tests, build · image build
and a smoke test of the running container.

## What is not tested

| Gap | Why it matters |
|---|---|
| **The local model's accuracy** | Unit tests use a scripted stand-in; the evaluation with a real model has not been run (no model registry access in the build environment) |
| Type checking of the domain packages | `mypy src` reports 254 errors outside the gated packages (agent 131, structure 50, timeseries 30, trends 18, hygiene 14, common 11). Most are untyped containers and a mixin pattern; one real bug was found this way in the gated code, so more may exist |
| Browser tests | No Playwright: real drag-and-drop uploads and downloads in a browser are unverified |
| Load and concurrency | No test with many simultaneous analysts or sustained job throughput |
| Restore drill | Backup commands are documented, not exercised |
| LibreOffice conversion in the container | The image layer could not be built in the audit environment |
| CI itself | The workflow file has not run on GitHub yet |

## Regression policy

A fixed defect gets a test that fails on the old code. The November 2026 audit's are in
`tests/test_regressions_audit.py` and `test_api.py::test_every_file_names_its_phase…`.
