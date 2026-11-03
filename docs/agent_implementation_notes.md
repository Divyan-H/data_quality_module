# Fix-with-agent: implementation notes

**Status:** built for Phase B (cell hygiene) and Phase T (Transformations), Nov 2026. Design and roadmap:
`docs/agent_architecture.md`. Phase T, its operations and the Porter-script mapping: `docs/transformations.md`.

## What the analyst sees

On Phase B's review screen there is a **Fix with agent** card. It opens a full-width screen with three panes:

| Pane | What it does |
|---|---|
| Open issues (left) | Needs_Review items grouped by rule and column, worst first. Clicking a group filters the sheet to its rows and shows one-click answers (Accept, Keep as is, Restore original, Split rows), which fill the chat box. |
| Sheet (centre) | The cleaned data, 100 rows a page, the way `Hygiene_Review.xlsx` shows it: green = fixed by a rule, blue = changed by the analyst or the agent, yellow = waiting for a decision. Hover a cell for the rule, the old value and why. Filters: all rows, changed, waiting. |
| Agent (right) | The chat. Every instruction comes back as a **preview card** (what will change in words, counts, up to 8 before/after cells, values it could not find, notes). Nothing changes until **Apply**. **Undo last change** restores the data as it was before the last applied change. |

Header buttons: Download workbook (the current `Hygiene_Review.xlsx`), Back to review, Accept and continue. Accepting is always a person's click.

## What it understands

Without any model (the command parser, `src/agent/planner/parser.py`):

| Say | Becomes |
|---|---|
| `accept all date fixes except row 14` · `reject the outlier fixes in Spend` · `keep row 33` · `keep the original values for outliers` | `resolve_findings` (Needs_Review Decision, in bulk) |
| `Head Office is Bangalore` · `change Sedan, Hatchback and 4dr Sedan to Passenger Car in Vehicle` · `TBD and TBC in Status mean empty` | `replace_values` (D-26 buckets). Without a column, the parser finds the column that holds the value; if several do, it asks |
| `remove '$' from Spend` | `replace_text` |
| `set Region to APAC where Country is India` · `in row 14 set City to Delhi` · `make Status Live for rows 2 to 5` · `fill empty Channel with Unknown` | `set_value` |
| `Spend is in thousands` · `divide Revenue by 100` · `multiply Spend by 1.18` | `scale` |
| `add column Revenue = Orders * AOV` · `add column Source with value Client` · `add column Medium from Channel: TV = Offline, others = Online` | `add_column` (formula, constant, or lookup) |
| `rename column Amt to Amount` · `remove rows where Status is Test` · `remove rows 4, 7` · `split the multi-value cells in City` | `rename_column`, `remove_rows`, `resolve_findings(split)` |
| `split Full Name into First and Last [on comma]` | `split_column` (new columns, checked by Phase B's rules) |
| `set Tier to Gold if Spend > 100000, Silver if Spend > 50000, else Bronze` | `classify` (if / else) |

Reshaping (unpivot, split rows, aggregate, joins …) is refused in Phase B with a pointer to Phase T, where the same chat
builds a recipe of such steps (`docs/transformations.md`). Conditions also compare dates: `Date >= 2025-09-09`.
| `summary` · `help` · `explain D-12` | an answer, numbers computed by code |
| `save recipe as monthly-fixes` · `use recipe monthly-fixes` · `list recipes` · `show steps` | recipe commands (both phases, below) |

Conditions: `<column> is <v>`, `is not`, `is one of a, b`, `contains`, `starts with`, `is empty`, `> < >= <=`, joined with `and`. Rows are Excel row numbers as the workbook shows them.

Anything else goes to the **local model** (Ollama, loopback only) when it is available: one schema-constrained call whose JSON schema lists the real operations, columns and rule codes, validated into the same typed operations. An invalid answer becomes a question, never a change. Without a model, the chat explains which commands it knows.

## Recipes (Phase B and Phase T)

A recipe is a named list of typed steps in `<workspace>/../recipes/<name>.json`, shared by every run (`src/agent/recipes.py`).
`use recipe <name>` turns its steps into one ordinary proposal: previewed, applied with a click, undoable.

* **Phase T** saves the run's transformation recipe as it stands.
* **Phase B** saves the steps of the change-sets applied in this run, in the order they were applied, **without** steps that name
  Excel rows (`in row 14 set City to Delhi`): row 14 of next month's file is a different row. The save message says how many were left
  out. `resolve_findings` steps are kept: next month they answer that month's findings of the same rule and column.
* A recipe belongs to its phase: using a Phase T recipe in Phase B is refused with a message naming the right phase.

## How it works

```
message ──> worker job "agent_message" (request waits)
              load tables -> Planner (parser, then LLM) -> typed Plan
              -> HygieneWorkspace.preview: run the plan on a throw-away load, through EditSession -> preview
              -> ChangeSet saved as proposed (run/agent/B/changesets/cs-NNNN.json)
apply   ──> worker job "agent_apply" (like a review upload: phase running -> review)
              version check (stale proposals are refused) -> fresh load -> same plan
              -> recheck / add_columns / split_rows (the review-upload code) -> snapshot -> save
              -> later phases discarded (orchestrator _clear_from), other proposals marked stale
undo    ──> worker job "agent_undo": restore the snapshot of the last applied change-set
```

Code map:

| Module | Role |
|---|---|
| `src/agent/operations.py` | The contract: 8 pydantic operation models + `RowFilter` / `Condition` + `Plan`. Nothing else can reach the data. |
| `src/agent/selectors.py` | Tables, columns (Phase B's header matching, so `city` finds `City_Name`), rows (conditions, Excel rows). |
| `src/agent/session.py` | `EditSession`: every write goes through it; it audits exactly like a workbook edit (`action: analyst_edit`, `rule: agent`, plus `changeset`) and records the preview. |
| `src/agent/executors.py` | One function per operation. Safe formula evaluation (AST whitelist, Decimal arithmetic). |
| `src/agent/describe.py` | Plain-language text of operations and previews, written by code. |
| `src/agent/workspace.py` | `Workspace` interface; `HygieneWorkspace` reuses `src/hygiene/review.py` (load, recheck, add_columns, split_rows, save), so chat and workbook give the same result. Snapshot / restore for undo. |
| `src/agent/planner/` | `context.py` (what a planner may know), `parser.py` (deterministic commands), `llm.py` (schema-constrained local model), `__init__.py` (parser first, then model). |
| `src/agent/store.py` | Change-sets, conversation and snapshots as files in the run folder (atomic writes). |
| `src/agent/service.py` | Use cases: message, apply, undo, discard. |
| `src/agent/views.py` | Read-only grouped issues and row pages for the UI (no worker job). |
| `src/orchestration/agent.py` | When the agent may act, which job runs, what an applied change means for the run's state. |
| `frontend/src/components/agent/` | `AgentWorkspace`, `IssueList`, `SheetView`, `ChatPanel`. |

API (`/api/v1/runs/{run}/phases/{key}/agent…`): `GET` state · `POST messages {text}` · `POST changesets/{cs}/apply` · `POST changesets/{cs}/discard` · `POST undo` · `GET issues` · `GET rows?table=&offset=&limit=&rule=&column=&only=changed|flagged`. Other phases answer `400 agent_not_available`.

Settings (`config/settings.yaml`, `service.agent`): `llm: on|off`, `model` (default: `service.llm.model`; a 7B+ instruct model with structured output is recommended, e.g. `qwen2.5:7b-instruct`). Env: `DQ_AGENT_MODEL`.

## Guarantees and their tests

| Guarantee | Where | Test |
|---|---|---|
| A preview changes nothing | `HygieneWorkspace.preview` loads a throw-away copy | `test_agent_api.py::test_an_instruction_is_previewed_applied_audited_and_undone` |
| What is applied is what was previewed | Same deterministic plan; base-version check refuses stale proposals | `test_agent_core.py::test_preview_changes_nothing_and_apply_matches_the_preview`, `test_a_stale_proposal_is_refused` |
| Every change is audited with its change-set | `EditSession._audit` | `test_every_change_is_audited_with_the_changeset` |
| Undo restores the exact previous state | snapshot before save | `test_service_round_trip_with_undo`, API undo test |
| Later phases never use outdated data | `AgentController.finished` -> `_clear_from` | `test_an_applied_change_discards_later_phases` |
| A workbook upload and agent history do not conflict | uploads mark proposals stale and end undo | `test_a_review_upload_after_agent_changes_keeps_history_but_ends_undo` |
| The model cannot invent columns or operations, or write | enum schema, pydantic validation, no write tool | `test_an_invalid_model_answer_becomes_a_question_not_a_change` |
| Formulas cannot run code | AST whitelist | `test_formula_rejects_anything_but_arithmetic` |
| Cell text is data, never instructions | values only as JSON strings inside CONTEXT; prompt rules | prompt in `planner/llm.py` |

## Changed outside `src/agent`

- `src/hygiene/review.py::add_columns` no longer re-runs D-32 (expected columns) on added columns: it judged the whole sheet and flagged its missing columns a second time (seen when the agent added a column; the same happened for columns pasted into the workbook).
- `src/structure/llm/client.py::OllamaClient.chat` takes optional `num_predict` and `system` (the agent needs a longer answer); defaults are unchanged for Phase A.
- `src/hygiene/config.py::RULE_TEXT` has an `agent` entry (shown in the workbook's Auto_Fix_Log).
- The review panel's summary reloads after agent changes.

## Not built yet (see `docs/agent_architecture.md` §7–§10)

- Agent support for Phases A, C and D (mark valid, add missing weeks, thresholds, structure overrides). The `Workspace` interface and `Phase.supports_agent` are the extension points.
- An evaluation set of real analyst instructions and the model comparison.
- Streaming instead of polling; login and per-user approval records.
