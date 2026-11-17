# Room for improvement

What is still not good enough, in priority order, as of 2026-11-17. Each item says what is wrong today, why it
matters and roughly how big the fix is (S = days, M = 1–2 weeks, L = a month or more). Nothing here is hidden
elsewhere: this file replaces the open lists in `docs/service_issues_and_roadmap.md` and
`docs/audit/02_final_assessment.md` as the one place to look.

Current state in one line: **fit for a controlled pilot on one server for one trusted team; not yet fit for wider
production.** The first three items are the reasons.

## 1. Blocks wider production

| # | Gap | Why it matters | Fix | Size |
|---|---|---|---|---|
| 1.1 | **The agent's local model has never been measured.** Only the command parser is (98% of documented commands, 20% of free wording, 0 wrong changes on 110 cases) | Nobody knows how often a real model plans a paraphrased instruction correctly, or which model to buy hardware for | Run `python -m src.agent.evaluation --llm on --model <name>` on the target machine for `llama3.2:3b`, `qwen2.5:7b-instruct` and a 14B model; record the table in `docs/AI_AGENT.md`; pick the model from it | S (needs a machine with Ollama) |
| 1.2 | **No user accounts.** One shared access token | The audit trail records actions, not people; everyone with the token sees every client's runs; "a Reviewer must approve" cannot be enforced | Single sign-on (OIDC), roles (Analyst, Reviewer, Admin), `actor` = the person | M |
| 1.3 | **No separation between clients** | A second client's data would be visible to the first client's analysts | `tenant` on every run and audit row, checked in one place; tests that try to read across clients | M (after 1.2) |
| 1.4 | **Most of the phase code has not been read in review.** 56% of `src` was covered by tools and tests only (`docs/audit/REVIEW_CHECKLIST.md`) | Three of the defects fixed so far were found only when a package was type-checked or read; unread code can hold more | Read `hygiene/review.py`, `structure/extract.py`, `hygiene/rules.py` first (the most complex functions) | M |
| 1.5 | **The full deployment has not been run once.** Verified: the image, the hardened container, proxy + service. Not verified: the model container, public TLS, a GPU, LibreOffice in the image | The first real deployment will be the first test of those parts | Bring the Compose stack up on the target server; run a restore drill; keep the notes in `docs/DEPLOYMENT.md` | S |
| 1.6 | **CI has not run on GitHub.** Every command was run by hand | The workflow file may differ from what GitHub's runners accept | Open a pull request and fix whatever fails | S |

## 2. Design limits (work as built, do not scale as built)

| # | Limit | Consequence | Better design | Size |
|---|---|---|---|---|
| 2.1 | Run state is one `state.json` per run under an in-process lock; the job table is in memory | **One service instance only.** No high availability; `list runs` reads every run's file | Runs, phases and jobs in PostgreSQL; a job queue with leases; then several API copies and worker machines | L |
| 2.2 | Each chat message starts a new worker process and reloads the tables | 1–3 s overhead per message, more on big tables | A long-running agent worker that keeps a run's tables in memory, keyed by data version | M |
| 2.3 | Undo copies the whole phase output per applied change (10 kept) | Disk use grows up to 10× a phase's output | Immutable dataset versions: apply writes version N+1, undo points back to N | M |
| 2.4 | Review items and the audit of Phase B are large JSON files | Slow to filter on big runs; whole file re-read on change | Parquet, read by page (the agent views already cache an index) | S–M |
| 2.5 | The browser polls every 1–6 s | Load grows with open screens | Server-sent events for run state and progress | S |
| 2.6 | Config checks and table-selection replays hold the HTTP request for up to 180 s | Timeouts behind strict proxies | Answer `202` and report through the run state, like phases do | S |
| 2.7 | Phase A keeps every sheet's cells in memory (about 1.4 GB per 400,000 rows) | Memory sets the largest file | Streamed extraction (spec gap G5) | L |
| 2.8 | Lockout counters are in memory | Reset on restart; per instance | Move with 2.1 | S |

## 3. Reshaping in Phase A (the newest part)

| # | Issue | Consequence | Fix | Size |
|---|---|---|---|---|
| 3.1 | Steps that change numbers (aggregate, allocate, net, convert currency, bucket others) run **before** the checks | For those tables Phases B–D check the result, not the client's rows: a duplicate or outlier inside a group is invisible to them. Today the app only asks for an explicit confirmation | Run Phase B's row-level rules on the original table first and carry the findings over through `lineage.parquet`; or split the recipe into "before the checks" and "after the checks" steps | M |
| 3.2 | Cell notes from Phase A (formulas, errors) do not carry over to a reshaped table | Rules D-05 to D-08 cannot fire on it | Map notes through the lineage for row-preserving steps | S–M |
| 3.3 | Columns the analyst pasted next to a table after Phase A are ignored for a table the recipe changed | They are silently not checked | Join them before the recipe runs, or refuse the paste with a message | S |
| 3.4 | Review workbooks of a reshaped table show the source Excel row only where one exists | Grouped rows have no row number | Show the lineage ids, or a link to the lineage file, in the workbook | S |
| 3.5 | The reshape screens were never clicked through in a browser | Layout or wording problems are possible | Browser tests (see 5.3) | S |

## 4. Agent

| # | Gap | Fix | Size |
|---|---|---|---|
| 4.1 | No read-only questions about the data ("how many blank cities in March?") | A `query` tool whose numbers are computed by code | M |
| 4.2 | An ambiguous name becomes a typed question, not a choice | Return the candidates; show them as buttons | S |
| 4.3 | Agent works in Phases A (reshape) and B only | `mark_valid`, `exclude_series`, `set_threshold` for C and D; structure overrides for A | L |
| 4.4 | No MCP adapter | Thin server over `registry.mcp_tools()` (read and propose only), when another AI app needs DQ | S–M |
| 4.5 | Nothing covers a transformation no operation expresses | A sandboxed, read-only SQL step with reviewer approval | M |
| 4.6 | Recipes cannot take values that change monthly (a rate, a start date) | Recipe parameters | S |
| 4.7 | The stricter privacy mode (no sample values in the prompt) exists in code but is not a setting | Expose it as `service.agent.privacy` | S |
| 4.8 | Phase A's model still reviews **every** decision by default although the project's own test showed a 3B model overruling the rules wrongly 14/14 times | Benchmark (`src/structure/llm/benchmark.py`), then default to `near_ties` or off | S |

## 5. Code quality and tests

| # | Debt | Evidence | Fix | Size |
|---|---|---|---|---|
| 5.1 | 123 type errors in the phase packages | `mypy src`: structure 50, timeseries 30, trends 18, hygiene 14, common 11. The service, security and agent packages are clean and gated in CI | Extend the gate one package at a time; each pass so far found a real bug | M |
| 5.2 | Very complex functions | `structure/extract.py::build_table` (complexity 65), `_select_columns` (59), `hygiene/review.py::apply_upload` (40), `precheck` (38) | Split along their natural steps, guarded by the existing tests | M |
| 5.3 | No browser-level tests | Folder drag-and-drop, downloads and the agent screens are untested in a real browser | Playwright against the built app | M |
| 5.4 | No load or soak test | Behaviour with many analysts or long job queues is unknown | A scripted load run on staging | S–M |
| 5.5 | `common/values.py` imports the date parser from `hygiene/values.py` | Breaks the "common depends on nothing" rule | Move the parser into `common` | S |
| 5.6 | One operation is still described in several places (model, executor, parser wording, `describe.py`) although the registry now owns phases, risk and the model's prompt | They can drift | Move wording and description into the registry entry | M |
| 5.7 | `openapi.json` is hand-shaped in places and the TypeScript types are written by hand | The web app and API can drift | Typed response models; generate the client | M |
| 5.8 | Test client uses `httpx`, which Starlette now marks as deprecated for this use | A future Starlette release drops it | Switch the test dependency when the replacement is stable | S |

## 6. Operations and security

| # | Gap | Fix | Size |
|---|---|---|---|
| 6.1 | No dashboards or alerts; metrics exist at `/api/v1/metrics` | Prometheus + Grafana, alerts on failed jobs, disk, model down | S |
| 6.2 | No tracing across API → worker → model | OpenTelemetry, reusing the request id | M |
| 6.3 | No virus scan of uploads | ClamAV at the proxy or in the upload path | S |
| 6.4 | LibreOffice runs inside the service container | A separate converter container with no network | M |
| 6.5 | Review workbooks and Phase B–D `pipeline.log` contain cell values (by design) | Disk encryption on the host; decide a rule for logs | S |
| 6.6 | Image tags are not pinned to digests | Pin in `docker-compose.yml`; rebuild deliberately | S |
| 6.7 | No external security review or penetration test | Schedule one before wider use | — |

## 7. Product

| # | Gap | Fix | Size |
|---|---|---|---|
| 7.1 | Phases E (mapping), F (DuckDB load) and G (pre-load checks) are specified, not built | `docs/architecture/specs/dq_e_*.md`, `dq_f_*.md`, `dq_g_*.md` | L |
| 7.2 | No client report (PDF) after Phase D | Build it from the `client_report` findings | M |
| 7.3 | No comparison of this month's run with last month's | Diff of findings by stable finding id | M |
| 7.4 | Per-client saved settings (fix switches, Phase C/D config, recipes offered automatically on a new file) | A project record once 1.2–1.3 exist | M |
| 7.5 | Repository still carries company-template leftovers (`prompts/`, `docs/governance/`, `docs/readmes/`) and 40+ Word files | Decide what to keep; move documents out of the code repository | S |

## Suggested order

1. 1.1 (measure the model), 1.5 (one real deployment and a restore drill), 1.6 (CI on GitHub): days, and they turn
   "not verified" into evidence.
2. 1.2 → 1.3 (identity, then client separation): the gate to more than one team.
3. 3.1 (checks and number-changing reshapes), 4.8, 5.1 and 1.4 alongside.
4. 2.1 → 2.2 → 2.3 when a second instance or large files are needed.
5. 7.1 when the end-to-end goal (data in DuckDB) is the priority.
