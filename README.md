# Data Pipeline service (Data Quality / ETL module)

Analyst web app and service for Phases A to D (structural intake, cell hygiene, time-series checks, trend checks).
Start it with `scripts\start.ps1` after `ollama serve`, open <http://127.0.0.1:8000>.
How it works, the API, decisions and known gaps: [docs/dq_service_implementation_notes.md](docs/dq_service_implementation_notes.md).
Each phase has its own notes in `docs/dq_[a-d]_implementation_notes.md`; the specs are in `docs/architecture/specs/`.

---

*Below: the original template README this repository was created from.*

# cuvity-project-template

**This is a template repository, not an application.** It contains no
working product code and none is intended to live here. Its only purpose
is to be copied — once, per new module-repo — as a starting skeleton for
teams using Claude as the primary generation engine, VS Code for editing
and Git, and GitHub for governance.

Do not build a product directly in this repo. Instantiate a new repo from
it, then build there.

## What's in this template

| Folder | Purpose |
|---|---|
| `/prompts` | Claude master prompt, reusable task-prompt library, usage guidelines |
| `/docs` | architecture, PRD, decision-log, and module-README templates, plus governance docs |
| `/src` | the pipeline service (see below) |
| `/tests` | pytest suite: unit, phase, API and end-to-end acceptance tests |
| `/config` | example application settings |

## Modules (`/src`)

- `structure/` — Phase A: finds the real tables in each file (headers, merged cells, gap rows, footers)
- `hygiene/` — Phase B: cell-level fixes and flags, with an audit of every change
- `timeseries/` — Phase C: per-series time checks (no-execution runs, repeats, duplicate / missing periods)
- `trends/` — Phase D: period-over-period trends, ratios, spend concentration
- `common/` — what the phases share: table loading, values, cadence, one folder per checked table (`units.py`)
- `orchestration/` — the A -> D state machine, worker processes, final files (`bundle.py`)
- `api/` — the HTTP API and the analyst web app (`python -m src.api`)
- `llm_interface/` — whether the local Ollama model is reachable
- `security/` — upload name and path checks
- `storage/` — run folders and their event log

## Using this as a template

1. Create a new repository from this template (or fork/copy it) under its
   real name — do not keep repo-name placeholders from this template's
   docs (e.g. `team_onboarding_guide.md`'s `cd <project-name>` step)
   unresolved.
2. Decide which modules from the menu above apply, delete the rest from
   `/src`, `/tests`, `CODEOWNERS`, `team_access_matrix.md`, and
   `high_level.md`.
3. Create the GitHub teams referenced in `docs/governance/CODEOWNERS`
   (`@tech-leads`, `@architects`, `@product-team`, `@engineering-team`,
   `@devops-team`, `@admins`) or replace those handles with the real
   ones for your org.
4. Follow `docs/governance/admin_setup.md` to configure CI/CD *before*
   enabling branch-protection status-check rules, then finish branch
   protection per `docs/governance/branch_protection_policy.md`.
5. Read `prompts/usage_guidelines.md` and
   `docs/governance/team_onboarding_guide.md` before generating any
   content with Claude.
6. Read `docs/governance/workflow.md` for the branching model and the
   per-module generation lifecycle.

## Governance

See `docs/governance/` for CODEOWNERS, branch protection policy, team
access matrix, workflow, onboarding guide, repo setup checklist, and admin
setup instructions. Template owner: Raghavan.

## Template versioning

See `TEMPLATE_CHANGELOG.md` for what's changed in this template over time.
When you fork/instantiate it, note which version you started from in your
new repo's own `docs/decisions/decision_logs.md`.
