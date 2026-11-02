# Agent build plan: local models only

**Goal:** a chat assistant over the existing pipeline that explains results, drafts review answers and applies cell edits, running entirely on a local model through Ollama.

## Design rule for a small local model

A 7B to 14B model is good at narrow, well-defined jobs and unreliable at long, open-ended tool use. So **the code drives, the model assists**:

- A fixed state machine decides the next phase and which actions are allowed. The model never chooses the pipeline route.
- The model does only three jobs: (1) turn a message into one of about 10 structured intents, (2) draft suggestions for review items, (3) phrase explanations around facts the code supplies.
- Output is **schema-constrained** (Ollama structured output), so a reply is always valid JSON or it is rejected.
- Every number, row ID and file name comes from the tools, never from the model.
- Every change needs a click on a confirmation card. If the model is unsure, it asks, or the UI shows buttons.
- If Ollama is down, the app works as it does today (no chat), as it already does for Phase A.

**Model:** start with `qwen2.5:7b-instruct` (already used here). Try a 14B or larger model if the GPU allows. Pick by the evaluation set below, not by reputation.

## Stages

| # | Weeks | Build | Gate to pass before the next stage |
|---|---|---|---|
| 0 | 1 | Event streaming; tool layer (Python functions with JSON schemas over `/api/v1`); decision and audit table; **evaluation set**: 50 labelled messages and 30 past review cases | Eval harness runs in CI |
| 1 | 2 | Read-only assistant: intent classification, result cards, "why was this flagged?" answered from the findings files | Intent accuracy 95% or better; no wrong numbers (all templated) |
| 2 | 3 | Draft and confirm: drafts review answers from this client's history; `edit_cell(row_id, column, value, reason)` with type validation, an override layer (raw upload never changed), re-run of later phases, audit entry | 80% or more of drafts accepted unchanged; edits hit the right cell 100% of the time (ambiguous requests must trigger a question) |
| 3 | 3 | Chat panel, pipeline canvas, inline review grid (front-end, can start during stage 1) | Usable end to end on three real files |
| 4 | later | Autonomy levels 1 and 2; database state, queue and login | Only if stage 2 gates held for a month of real use |

Total for stages 0 to 3: about 8 to 10 weeks for one developer.

## What to measure

Intent accuracy, share of drafts accepted unchanged, minutes saved per run against the Excel workflow, slowest 5% of reply times, and number of "I didn't understand" fallbacks.

## Risks specific to local models

- **Slower and weaker reasoning.** Keep prompts short, send summaries not tables, cache repeated answers, and show progress while it thinks.
- **Wrong or invented facts.** Prevented by templating all numbers and quoting source cells.
- **Instructions hidden in cell values.** Cell text is passed as quoted data only; the model has no tool that can run on its say-so alone.
- **Hardware.** Replies on a small GPU can take many seconds. Measure on the real machine in stage 0.

## Decision rule

If the 7B model misses a gate, test a larger model. If that also misses, keep the chat as a **command palette** (fixed phrases plus buttons, no free drafting). It is still useful, and the rest of the plan does not depend on it.
