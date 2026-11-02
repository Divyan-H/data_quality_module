# Git Workflow — Claude-only

## Branching Model
- main → production
- dev → integration
- feature/<module>/<task> → individual work

Examples:
```
feature/ingestion/add-s3-loader
feature/analytics/add-forecasting-engine
feature/llm_interface/add-prompt-router
```

## Repo-Level Workflow
1. Create feature branch
2. Use Claude (with master_prompt.md) to generate PRDs, architecture, code, tests
3. Paste output into VS Code, edit as needed
4. Commit changes
5. Push branch
6. Open Pull Request
7. CODEOWNERS auto-assign reviewers
8. CI/CD runs tests
9. Reviewers approve
10. Merge into dev
11. Scheduled merge from dev → main

## Module-Level Workflow
Apply the relevant steps of this lifecycle to each module you actually kept
in `/src` for this repo (see `README.md` → Module menu — most repos keep a
subset of ingestion, processing, storage, analytics, llm_interface, api,
orchestration, monitoring, security, utils, not all ten):

1. **Define module scope** — use Claude to produce purpose, inputs, outputs,
   dependencies, constraints, non-goals. Becomes the module README intro.
2. **Generate module architecture** — component breakdown, data flow,
   interfaces, error handling, logging, security. Save to
   `docs/architecture/<module>_architecture.md`.
3. **Create module PRD** (if the module has business logic, e.g. analytics,
   API, LLM interface). Save to `docs/prd/<module>_prd.md`.
4. **Generate module code skeleton** — main class/function, interfaces,
   error handling, logging, config hooks. Save to `src/<module>/`.
5. **Generate module tests** — unit tests, integration tests, mocking
   strategy, edge cases. Save to `tests/`.
6. **Generate module README** — what it does, how to run it, inputs/outputs,
   common errors, extension points. Save to
   `docs/readmes/<module>_readme.md`.

Steps 2 and 6 are optional for a trivial module with no independent design
and no external interface (e.g. `utils/`) — a short section in the parent
README can cover it instead. Step 3 stays conditional as written above.
Steps 1, 4, and 5 apply to any module you keep, regardless of size.

7. **Create feature branch**: `feature/<module>/<task>`.
8. **Paste Claude output into VS Code**, edit, run tests locally if possible.
9. **Push branch**.
10. **Open Pull Request** — CODEOWNERS auto-assign reviewers.
11. **Review + CI/CD** — PR cannot merge until CODEOWNERS approve, CI passes,
    no conflicts, branch protection rules satisfied.
12. **Merge into dev**, then scheduled merge into `main` by a Tech Lead or
    Admin (see `team_access_matrix.md`).

## Rules
- No direct commits to main or dev
- All changes must go through PRs
- All PRs must pass CI
- All PRs must have required approvals
- All PRs must follow folder ownership rules (see CODEOWNERS)

## Claude Workflow
- Claude generates PRDs, architecture, code modules, tests, READMEs
- All generated files go through PR review before merging
