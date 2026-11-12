# Data Pipeline (DQ) service

Checks client spreadsheets for data-quality problems in phases, with a person reviewing each phase, and a chat agent
that turns plain-English instructions into previewed, approved, undoable changes. Everything runs on your own
machine or server; the agent uses a **local open-source model** (Ollama) and no client data leaves the host.

| Phase | What it does |
|---|---|
| A · Structure | Finds tables, headers and footers inside messy workbooks |
| B · Hygiene | 33 cell rules: dates, types, spellings, units, duplicates, negatives, multi-value cells |
| C · Time series | Missing periods, repeats, cadence breaks |
| D · Trends | Spikes, level shifts, monthly-to-weekly |
| T · Transformations (optional) | Reshapes tables from a recipe: unpivot, split, allocate, aggregate, join |
| Fix-with-agent | "Spend is in thousands" → typed operation → preview → Apply → Undo |

## Quick start (your own machine)

Needs Python 3.12+ and Node 22. Ollama is optional: without it the chat still understands its built-in commands.

```bash
python -m venv .venv
```

```bash
source .venv/bin/activate        # Windows: .venv\Scripts\Activate.ps1
```

```bash
pip install --require-hashes -r requirements-dev.txt
```

```bash
sh scripts/start.sh              # Windows: .\scripts\start.ps1
```

Open <http://127.0.0.1:8000>, upload `samples/phase_b_issues.xlsx`, run Phase A and B, then click **Fix with agent**.

## Run it on a server (Docker)

```bash
cp .env.example .env             # set DQ_API_TOKEN, STRUCTURE_HASH_KEY, DQ_DOMAIN
```

```bash
docker compose up -d --build
```

```bash
docker compose --profile setup run --rm model-pull qwen2.5:7b-instruct
```

Details, backups, upgrades and rollback: [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

## Documentation

| Read this | For |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How the parts fit, data flow, design decisions |
| [docs/PROJECT_STRUCTURE.md](docs/PROJECT_STRUCTURE.md) | What lives where, dependency rules |
| [docs/AI_AGENT.md](docs/AI_AGENT.md) | The agent: tools, guardrails, privacy, evaluation, model choice |
| [docs/API.md](docs/API.md) | Every endpoint, error shape, examples |
| [docs/DATABASE.md](docs/DATABASE.md) | Where state lives: run folders and the audit database |
| [docs/SECURITY.md](docs/SECURITY.md) | Threat model, controls, what is not covered |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | Docker Compose deployment, backup, rollback |
| [docs/ENVIRONMENT_VARIABLES.md](docs/ENVIRONMENT_VARIABLES.md) | Every setting |
| [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) | Local setup, adding a rule / operation / phase |
| [docs/TESTING.md](docs/TESTING.md) | Test layers, gates, what is and is not verified |
| [CHANGELOG.md](CHANGELOG.md) | What changed |
| [docs/audit/](docs/audit/) | The November 2026 engineering audit and readiness assessment |

Phase specifications are in `docs/architecture/specs/`; per-phase implementation notes in `docs/dq_*_implementation_notes.md`.

## Status

Single-server deployment, one shared access token, human approval on every change. Not yet built: per-user accounts
and client separation, multi-server operation, Phases E–G (mapping, load, pre-load checks). The honest list is in
[docs/audit/02_final_assessment.md](docs/audit/02_final_assessment.md).
