# Data Pipeline: open items and next steps

Status as of 2026-10-21. Phases A to D and the analyst web app are built and tested. This note lists what is still open, module by module, and the three
directions you raised: where mapping goes, a conversational UI, and an agentic pipeline. Detail for defects and security is in
`docs/service_issues_and_roadmap.md`.

---

## 1. The big picture

| Stage | What it does | Status |
|---|---|---|
| A. Structure | Finds the real tables in messy workbooks | Built |
| B. Values | Fixes and flags every cell; rule-based, no AI | Built |
| C. Time series | Gaps, repeats, zeros, order | Built |
| D. Trends | Jumps, outliers, spend versus result | Built |
| **E. Mapping** | Source columns and values to the target schema | **Not built** (spec exists) |
| **F. Load** | Parquet into DuckDB, full refresh | **Not built** (spec exists) |
| **G. Pre-load checks** | Schema, grain and size gate before the load | **Not built** (spec exists) |
| Web app and API | Upload, run, review, download | Built, UI not final |
| Platform fit | Login, tenants, durable state, queue | **Not built** |

The service currently ends at Phase D with clean Parquet and reports. The product goal (data in DuckDB) needs E, F and G.

---

## 2. Open items by module

**Phase A, structure**
- The local model overruled the rules 14 times out of 14 in the live test and was wrong each time. Benchmark a larger model on real files before trusting it.
- Re-uploading a review re-runs whole files; make it per table.
- Large sheets are held in memory and stop at a size limit.
- Spec contradiction (distinct labels versus the golden fixture) needs a decision; code comments cite old section numbers.

**Phase B, values**
- D-26 value consolidation upload (group raw category values into one bucket) is not built.
- D-16 data dictionary has no screen.
- One review row per flagged cell: a noisy column gives thousands of rows. Group by column and rule, with an "apply to all" answer.

**Phase C, time series**
- N-08 repeated values is pairwise: a constant series gives tens of thousands of rows. Change to one row per value.
- Checks are written for weekly data; decide what each check means for daily and monthly data.
- Config steps are slow on large tables; profile a sample.
- Open decision: set `timeseries.require_geography` to `false`, otherwise daily files without a geography column are rejected.

**Phase D, trends**
- Client-report PDF after Phase D is not built.
- Uncommitted changes to review: derived Period for Phase D, Year and Month excluded from variables, shared period-column function.

**Phase E, mapping** (see section 3)
- Everything: column mapping (E-02, E-06, E-14), value mapping (E-03, E-04, E-05), include or drop per column.

**Phase F, load**
- Everything: full-refresh load into DuckDB inside one transaction, with post-load NULL check.

**Phase G, pre-load checks**
- Everything: schema (E-11), grain (E-16), isolation and size (E-10), and a `load_gate_report.json` that F obeys.

**Web app and API**
- UI not finalised (see section 4).
- Review still goes through Excel round trips; an in-app grid would help.
- Browser-level tests are missing (drag and drop, downloads).
- Polling every second; server-sent events would be cleaner.
- Typed API models and a generated frontend client.

**Platform**
- No login and no tenant separation. Do not host it on a shared server yet.
- State sits in one process on one disk; needs a database and a queue.
- Job Contract covers Phase A only; B to D (and later E to G) need it.
- No metrics, structured logs or retention rules.
- Uncommitted work since the last push: granularity, skip, daily-data fix, docs. Commit it.

---

## 3. Where the mapping file goes

**Recommendation: a new Phase E straight after Phase D, driven by one saved mapping file per client.**

Why there: names and values are only trustworthy after A (real headers) and B (clean values), and C and D should keep working on the client's own column names.
Mapping earlier would force the checks to run on renamed columns.

How it works, the same way C and D already work:
1. The service reads the Phase D tables and **proposes** a mapping: each source column to a target column (fuzzy name match), each distinct value to a canonical member, and an Include or Drop flag.
2. The analyst downloads it as an Excel file (three sheets: Columns, Values, Include/Drop), edits it, and uploads it. The upload is validated first.
3. The service applies it and writes the mapped Parquet for F and G. Unresolved values block the load (E-04).
4. The mapping is **saved per client**. On the next run it is applied automatically and the analyst only sees what is new or changed.

Decisions needed from you:
- Who owns the target schema and its dimension lists (channel, geography, brand)? The spec says this service defines them, but someone has to maintain them.
- Is the mapping per client, per source kind, or both?
- Should a repeat run apply the saved mapping silently, or always ask for confirmation?

---

## 4. A conversational UI

The idea: a chat as the front door, with the current screens kept as the detail view and the fallback.

What the chat does:
- The analyst drops files into the chat. The assistant replies with the plan: "I see 3 workbooks, 5 tables. Starting Phase A."
- Progress appears in the thread. When a phase finishes, the assistant posts a **result card** (counts, top issues, a download button) and asks what to do.
- The analyst answers in words: "accept all date fixes except row 14", "skip C and D", "map CITY to Geography".
- Edits to review items happen in the thread or in an inline table, with the Excel file still available as the audit copy.

What it needs:
1. A thin **tool layer** over the existing `/api/v1` endpoints (create run, start phase, summary, review, accept, skip, config). The API is already step-based, so this is mostly wiring.
2. Streaming updates (server-sent events) in place of polling.
3. Conversation memory per run, and per client for saved choices.
4. Structured cards for results rather than free text, so numbers always come from the service and never from the model.
5. **Confirmation for anything irreversible** (accept, restart, delete, load to the database).
6. Keep the existing wizard so a user can always see the exact state.

Risk: a model that "summarises" results can misstate them. Rule: the model writes the sentence, the service supplies every number and file.

---

## 5. Making it an agentic pipeline

Today a person clicks through each phase. An agent can do the routine steps and stop where judgement is needed.

**Design**
- The agent has the API endpoints as tools (an MCP server or function calls). It decides what to run next, reads the summaries, and prepares the review or config for the analyst.
- **The phases stay deterministic.** The agent never edits data. It only starts phases, proposes review answers and configs, and asks questions.
- Every agent decision is written to the run log with its reason, the same as today's rule decisions.

**Autonomy levels** (pick per client):
| Level | Behaviour |
|---|---|
| 0 Suggest | Agent explains and recommends; the analyst clicks. |
| 1 Run and propose | Agent runs phases and drafts review answers; the analyst accepts each phase. |
| 2 Auto-accept when clean | Agent accepts a phase itself if there are no Needs_Review rows, no critical findings and no fallback; otherwise it stops and asks. |

**Where an agent helps most**
- Phase E: propose the column and value mapping, and learn from past approvals for that client.
- Phases B and D: draft answers for Needs_Review rows and thresholds from the client's history, for the analyst to confirm.
- Reports: write the plain-language client summary from the findings files.
- Failures: explain a failed phase and suggest the fix.

**Guardrails**
- A load to the database (Phase F) always needs a person, whatever the level.
- Same file in, same result out must still hold: the agent's choices are recorded, so a run can be replayed.
- An evaluation set of past runs with known correct answers, run whenever the model or prompt changes. The Phase A model result above shows why.
- Data stays local: the model runs on the same machine unless a decision is made otherwise.

**Prerequisites**: durable run state and a queue (so the agent can resume), the Job Contract for B to D, login and tenants, and the mapping memory per client.

---

## 6. Suggested order

1. **Now:** commit the pending work, settle `require_geography`, fix the Phase C N-08 and Phase B row grouping so outputs are usable.
2. **Phase E mapping** with the saved per-client mapping file.
3. **Phases G then F** (gate first, then load), with the full-refresh transaction.
4. **Conversational UI** over the existing API, including streaming and result cards.
5. **Agent layer**: start at level 1 for mapping and review drafting; add evaluation runs.
6. **Platform fit**: login, tenants, database state, queue, contract for every phase, metrics.
7. **Client report PDF** and per-client saved settings.
