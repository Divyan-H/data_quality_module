# Final assessment (Phase 9): what was done, what is verified, what is not

Date: 2026-11-12. Branch `production-hardening`. Starting point: `b357191`. Companion documents:
`01_initial_assessment.md` (findings and plan) and `REVIEW_CHECKLIST.md` (what was actually inspected).

## Verdict

**Ready for a controlled pilot on one server, behind the reverse proxy, for one trusted team. Not ready for wider
production use.** Three things block that, and none of them could be closed in this engagement:

1. **The agent's local model has never been measured.** The design is sound and the non-model path is measured,
   but nobody knows yet how often a real model plans a paraphrased instruction correctly.
2. **There are no user accounts.** One shared token means the audit trail records actions, not people, and every
   holder sees every client's runs.
3. **Only 18% of the code was read line by line** (44% in full or in part). The rest was covered by tools and tests.

## Findings from the audit and their state

| # | Finding | State | Evidence |
|---|---|---|---|
| F-01 | Job Contract path traversal | **Fixed** | 6 regression tests; reproduction no longer writes outside the root |
| F-02 | Results destroyed by a refused restart | **Fixed** | Regression test with a real busy service |
| F-03 | Regex denial of service | **Fixed** | Time- and length-limited matching; regression tests |
| F-04 | Unvalidated `/jobs` body, unbounded job table | **Fixed** | Typed body; table bounded at 500; tests |
| F-05 | No security headers, request ids, error handler, structured logs | **Fixed** | Tests; headers seen on the running container and through the proxy |
| F-06 | Sign-in guessing not limited | **Fixed** (single instance, in memory) | Lockout tests |
| F-07 | Unpinned dependencies | **Fixed** | Hash-pinned files; full suite passes on the pinned set in a clean environment |
| F-08 | Agent: no evaluation, memory, call audit, single definition, repair | **Fixed, except the model measurement** | Registry, guardrails, memory, repair, audit, 110-case evaluation |
| F-09 | Single instance, no audit store | **Partly**: audit database added; single instance remains, documented | `docs/ARCHITECTURE.md` "Known limits" |
| F-10 | No deployment files, no graceful drain | **Fixed** | Image built and run; Compose proxy + service run; drain tested |
| F-11 | 260 type errors | **Partly**: infrastructure packages clean and gated; 254 remain in domain and agent code | `mypy` output in `docs/TESTING.md` |
| F-12 | Vulnerable frontend dev tools | **Fixed** | `npm audit`: 0 |
| F-13 | Very complex functions | **Not addressed** (deliberately: high regression risk) | Listed as debt |
| F-14 | Development hash key fallback | **Fixed for production** | Start-up refusal seen on the container |
| F-15 | Phase A's model reviews every decision | **Not changed** (it was an explicit product choice); flagged | — |
| F-16 | Template README, lock files | **Fixed** (template folders kept) | — |
| F-17 | Undo copies the phase folder | **Not addressed** | Listed as debt |
| new | Phase C/D artifacts carried the description as the phase | **Fixed** | Found by the type check; regression test fails on the old code |
| new | `rename column X to Y in <table>` | **Fixed** | Found by the evaluation set |

## Readiness by area

Classification: **Verified** = exercised and passing here; **Needs improvement** = works, with a known gap;
**Not verified** = built but not exercised; **Blocked** = could not be done in this environment.

| Area | Rating | Basis |
|---|---|---|
| Phase logic A–D, T | **Verified** by tests; **not re-audited** by reading | 730 tests pass; 56% of `src` unread (see checklist) |
| Orchestration and jobs | **Verified** | Read in full; cancel, crash, timeout, limit, recovery, drain tests |
| API layer | **Verified** | Read in full; one error shape; validation; typed and gated |
| Authentication | **Needs improvement** | Token, cookie and lockout verified; no users, roles or client separation |
| Web security headers, CSRF, traversal, upload validation | **Verified** | Tests; seen on the running container and through the proxy |
| Agent: safety design (typed tools, preview, guardrails, phase limits) | **Verified** | Tests; 20/20 must-not-change cases; 0 wrong changes in 110 cases (parser) |
| Agent: accuracy with the local model | **Blocked** | The model registry was unreachable; run `python -m src.agent.evaluation --llm on` on the target machine |
| Agent: privacy (local only, host allow-list, nothing sensitive stored) | **Verified** in code and tests; network isolation of the model container **not verified** | Allow-list test; audit-content tests; Ollama container not run |
| Audit trail | **Needs improvement** | Complete for actions and model calls; cannot name a person |
| Data storage | **Needs improvement** | Atomic and tested for one instance; no second instance possible; restore not drilled |
| Observability | **Needs improvement** | JSON logs, request ids, metrics endpoint verified; no dashboards or alerts set up; no tracing |
| Dependencies and supply chain | **Verified** | Hash-pinned; `pip-audit`, `npm audit`, `bandit` clean |
| Container image | **Verified** without LibreOffice; LibreOffice layer **Blocked** | Built, run hardened, full flow inside it; Debian mirrors unreachable for the optional layer |
| Compose: proxy + service | **Verified** locally | Redirect, HSTS, token, body limit, hidden metrics, upload |
| Compose: model container, GPU, public TLS | **Not verified** | Not run |
| CI workflow | **Not verified** | Each command was run by hand here; the workflow has not run on GitHub |
| Frontend | **Needs improvement** | 80 component tests, type-checked, builds; no browser tests; accessibility not audited |
| Performance and load | **Not verified** | No load test; existing performance tests (`-m perf`) not run in this audit |
| Backup and recovery | **Not verified** | Commands documented; no drill |
| Type safety | **Needs improvement** | 5 packages gated; 254 errors elsewhere |
| Documentation | **Verified** against the code at this commit | Endpoint and variable lists generated from the code |

## What changed, in numbers

| | Before | After |
|---|---|---|
| Backend tests | 679 | 730 |
| Frontend tests | 78 | 80 |
| Confirmed defects open | 5 (2 high) | 0 |
| Dependencies pinned | 0 | all, with hashes |
| Known vulnerabilities (pip / npm) | 0 / 5 | 0 / 0 |
| Type errors in gated packages | 32 | 0 |
| Agent evaluation cases | 0 | 110 |
| Deployment files | none | Dockerfile, Compose, proxy config, CI |

## Remaining work, in order

1. **Measure the model.** Run the evaluation with `llama3.2:3b`, `qwen2.5:7b-instruct` and a 14B model on the
   target hardware. Fix the model and prompt from the result. Until then, treat free wording as experimental.
2. **Run the pilot stack once for real**: Compose with Ollama, a pulled model, a real certificate; then a restore drill.
3. **Push and let CI run**; fix whatever differs on GitHub's runners.
4. **Identity** (single sign-on), roles, client separation. This is the gate to more than one team.
5. **Read the unread code**, starting with `hygiene/review.py` and `structure/extract.py` (the most complex
   functions), and extend the type gate package by package.
6. Browser tests; a load test; dataset versions instead of folder snapshots; PostgreSQL when a second instance is needed.

## Decisions still needed from the owner

The five questions in `01_initial_assessment.md` §8 were not answered; work proceeded on the stated defaults
(single server with Compose, shared token, model left configurable, template folders kept, no PostgreSQL).
