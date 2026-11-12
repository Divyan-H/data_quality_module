# Review checklist: what was and was not inspected

The brief asked for every line to be reviewed, and for no claim of review that was not done. This is the record
for the November 2026 audit. **Not every line was read.**

| Level | Files | Lines | Share of `src` |
|---|---|---|---|
| Read in full, line by line | 28 | 4,267 | 18% |
| Read in part (the sections named below) | 22 | 6,291 | 26% |
| Not read in this audit; covered by tool scans and the test suite only | 80 | 13,204 | 56% |

**Every file** was covered by: `ruff` (bug and security rules), `bandit`, `vulture` (dead code), `radon`
(complexity), `mypy`, and the 730-test suite.

**What "not read" means.** The domain packages (`structure`, `hygiene`, `timeseries`, `trends`, most of `common`)
were prioritised below the service, security and agent code because they sit behind process isolation, are the most
heavily tested part of the project, and were not where the confirmed defects were. That is a judgement, not a
guarantee: the one bug found by type-checking (`phases/timeseries.py`) shows that unread code can hold defects.
Much of `src/agent` was written in earlier sessions of this engagement and re-read only where listed.

**Frontend.** Read in full: `api.ts`, `components/SignIn.tsx`, the proposal cards in
`components/agent/ChatPanel.tsx`, `components/agent/AgentWorkspace.tsx` (apply path). The rest of `frontend/src`
(about 3,900 lines) was covered by the TypeScript compiler and the 80 component tests, not read.

**Tests.** Test files were read where they were changed or failed; they were not audited for quality.

## Per file

| File | Lines | Inspection | Notes |
|---|---|---|---|
| `src/agent/__init__.py` | 17 | Not read | tool scans only |
| `src/agent/describe.py` | 156 | Not read | tool scans only |
| `src/agent/errors.py` | 15 | Not read | tool scans only |
| `src/agent/evaluation.py` | 274 | Read in full | read line by line |
| `src/agent/executors.py` | 372 | Read in part | regex sites, formula allow-list and dispatch read |
| `src/agent/guardrails.py` | 61 | Read in full | read line by line |
| `src/agent/operations.py` | 429 | Read in part | class list and unions read; field validators not re-read |
| `src/agent/planner/__init__.py` | 59 | Read in full | read line by line |
| `src/agent/planner/context.py` | 194 | Read in part | constructor and pack() read |
| `src/agent/planner/llm.py` | 232 | Read in full | read line by line |
| `src/agent/planner/parser.py` | 549 | Read in part | rename, help text and dispatch read |
| `src/agent/planner/transform_commands.py` | 418 | Read in part | table helpers read |
| `src/agent/recipes.py` | 84 | Read in full | read line by line |
| `src/agent/registry.py` | 117 | Read in full | read line by line |
| `src/agent/safe_regex.py` | 61 | Read in full | read line by line |
| `src/agent/selectors.py` | 150 | Read in part | condition matching read |
| `src/agent/service.py` | 235 | Read in full | read line by line |
| `src/agent/session.py` | 199 | Not read | tool scans only |
| `src/agent/store.py` | 149 | Read in part | models and summary read; file helpers skimmed |
| `src/agent/transform/__init__.py` | 0 | Not read | tool scans only |
| `src/agent/transform/describe.py` | 28 | Not read | tool scans only |
| `src/agent/transform/engine.py` | 1001 | Read in part | Effect, regex sites, rename and dispatch read |
| `src/agent/transform/workspace.py` | 261 | Read in part | preview and FrameTable read |
| `src/agent/views.py` | 171 | Read in full | read line by line |
| `src/agent/workspace.py` | 138 | Read in part | interface and preview totals read |
| `src/api/__main__.py` | 23 | Read in full | read line by line |
| `src/api/api_server.py` | 229 | Read in full | read line by line |
| `src/api/v1.py` | 304 | Read in full | read line by line |
| `src/common/__init__.py` | 1 | Not read | tool scans only |
| `src/common/additive.py` | 246 | Not read | tool scans only |
| `src/common/columns.py` | 1 | Not read | tool scans only |
| `src/common/dataset.py` | 254 | Not read | tool scans only |
| `src/common/errors.py` | 2 | Not read | tool scans only |
| `src/common/granularity.py` | 217 | Not read | tool scans only |
| `src/common/logs.py` | 21 | Not read | tool scans only |
| `src/common/monthly_weeks.py` | 106 | Not read | tool scans only |
| `src/common/run_inputs.py` | 226 | Not read | tool scans only |
| `src/common/units.py` | 88 | Not read | tool scans only |
| `src/common/values.py` | 121 | Read in part | finding id read |
| `src/common/weekly_rollup.py` | 140 | Not read | tool scans only |
| `src/hygiene/__init__.py` | 1 | Not read | tool scans only |
| `src/hygiene/cities.py` | 200 | Not read | tool scans only |
| `src/hygiene/cli.py` | 50 | Not read | tool scans only |
| `src/hygiene/config.py` | 130 | Not read | tool scans only |
| `src/hygiene/io.py` | 240 | Not read | tool scans only |
| `src/hygiene/kpi.py` | 196 | Not read | tool scans only |
| `src/hygiene/pipeline.py` | 109 | Not read | tool scans only |
| `src/hygiene/report.py` | 28 | Not read | tool scans only |
| `src/hygiene/review.py` | 476 | Not read | tool scans only |
| `src/hygiene/rules.py` | 917 | Not read | tool scans only |
| `src/hygiene/table.py` | 189 | Not read | tool scans only |
| `src/hygiene/values.py` | 167 | Not read | tool scans only |
| `src/llm_interface/ollama_status.py` | 82 | Read in part | probe read; tail skimmed |
| `src/observability/__init__.py` | 1 | Read in full | read line by line |
| `src/observability/context.py` | 14 | Read in full | read line by line |
| `src/observability/http.py` | 84 | Read in full | read line by line |
| `src/observability/logs.py` | 47 | Read in full | read line by line |
| `src/observability/metrics.py` | 41 | Read in full | read line by line |
| `src/orchestration/__init__.py` | 0 | Not read | tool scans only |
| `src/orchestration/agent.py` | 254 | Read in full | read line by line |
| `src/orchestration/bundle.py` | 115 | Read in part | zip builder read |
| `src/orchestration/orchestrator.py` | 732 | Read in full | read line by line |
| `src/orchestration/phases/__init__.py` | 22 | Read in full | read line by line |
| `src/orchestration/phases/base.py` | 232 | Read in part | Artifact, Context and Phase interface read |
| `src/orchestration/phases/findings.py` | 44 | Read in part | lines flagged by the type check read |
| `src/orchestration/phases/hygiene.py` | 173 | Read in part | lines flagged by the type check read |
| `src/orchestration/phases/structure.py` | 261 | Read in part | lines flagged by the type check read |
| `src/orchestration/phases/timeseries.py` | 177 | Read in part | artifact builder read and fixed |
| `src/orchestration/phases/transform.py` | 104 | Not read | tool scans only |
| `src/orchestration/phases/trends.py` | 154 | Read in part | lines flagged by the type check read |
| `src/orchestration/runner.py` | 218 | Read in full | read line by line |
| `src/orchestration/settings.py` | 121 | Read in full | read line by line |
| `src/orchestration/sources.py` | 47 | Not read | tool scans only |
| `src/orchestration/worker.py` | 114 | Read in full | read line by line |
| `src/security/auth.py` | 199 | Read in full | read line by line |
| `src/security/uploads.py` | 75 | Read in full | read line by line |
| `src/storage/audit.py` | 172 | Read in full | read line by line |
| `src/storage/run_store.py` | 159 | Read in full | read line by line |
| `src/structure/__init__.py` | 4 | Not read | tool scans only |
| `src/structure/classify.py` | 138 | Not read | tool scans only |
| `src/structure/cli.py` | 61 | Not read | tool scans only |
| `src/structure/colors.py` | 76 | Not read | tool scans only |
| `src/structure/config.py` | 118 | Not read | tool scans only |
| `src/structure/decisions.py` | 99 | Not read | tool scans only |
| `src/structure/errors.py` | 20 | Not read | tool scans only |
| `src/structure/extract.py` | 800 | Not read | tool scans only |
| `src/structure/facts.py` | 171 | Not read | tool scans only |
| `src/structure/features.py` | 244 | Not read | tool scans only |
| `src/structure/findings.py` | 304 | Not read | tool scans only |
| `src/structure/header.py` | 249 | Not read | tool scans only |
| `src/structure/job.py` | 164 | Read in full | read line by line |
| `src/structure/llm/__init__.py` | 1 | Not read | tool scans only |
| `src/structure/llm/benchmark.py` | 78 | Not read | tool scans only |
| `src/structure/llm/client.py` | 108 | Read in part | client, host check and chat read; vote helpers skimmed |
| `src/structure/llm/examples.py` | 79 | Not read | tool scans only |
| `src/structure/llm/review.py` | 175 | Not read | tool scans only |
| `src/structure/llm/view.py` | 132 | Not read | tool scans only |
| `src/structure/model.py` | 111 | Not read | tool scans only |
| `src/structure/pipeline.py` | 645 | Read in part | hash-key handling read |
| `src/structure/readers.py` | 184 | Not read | tool scans only |
| `src/structure/regions.py` | 368 | Not read | tool scans only |
| `src/structure/review_import.py` | 422 | Not read | tool scans only |
| `src/structure/review_issues.py` | 200 | Not read | tool scans only |
| `src/structure/review_workbook.py` | 273 | Not read | tool scans only |
| `src/structure/selection.py` | 127 | Not read | tool scans only |
| `src/structure/sniff.py` | 91 | Not read | tool scans only |
| `src/structure/writer.py` | 144 | Not read | tool scans only |
| `src/structure/xlsx.py` | 518 | Read in part | XML parser settings and size limits read |
| `src/timeseries/__init__.py` | 1 | Not read | tool scans only |
| `src/timeseries/autoconfig.py` | 331 | Not read | tool scans only |
| `src/timeseries/checks.py` | 437 | Not read | tool scans only |
| `src/timeseries/cli.py` | 66 | Not read | tool scans only |
| `src/timeseries/config.py` | 253 | Not read | tool scans only |
| `src/timeseries/io.py` | 39 | Not read | tool scans only |
| `src/timeseries/layout.py` | 67 | Not read | tool scans only |
| `src/timeseries/pipeline.py` | 100 | Not read | tool scans only |
| `src/timeseries/report.py` | 250 | Not read | tool scans only |
| `src/timeseries/review.py` | 213 | Not read | tool scans only |
| `src/timeseries/values.py` | 15 | Not read | tool scans only |
| `src/trends/__init__.py` | 1 | Not read | tool scans only |
| `src/trends/autoconfig.py` | 302 | Not read | tool scans only |
| `src/trends/checks.py` | 561 | Not read | tool scans only |
| `src/trends/cli.py` | 68 | Not read | tool scans only |
| `src/trends/config.py` | 359 | Not read | tool scans only |
| `src/trends/io.py` | 124 | Not read | tool scans only |
| `src/trends/pipeline.py` | 119 | Not read | tool scans only |
| `src/trends/report.py` | 302 | Not read | tool scans only |
| `src/trends/review.py` | 248 | Not read | tool scans only |
| `src/trends/values.py` | 9 | Not read | tool scans only |
| `src/trends/weekly.py` | 99 | Not read | tool scans only |
