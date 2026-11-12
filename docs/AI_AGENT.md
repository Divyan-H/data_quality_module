# The AI agent

The agent turns an analyst's sentence into **typed operations**, shows a preview, and applies the change only after
a person approves it. It is a planner, not an autonomous editor: the model never writes data, runs code or accepts a
phase.

## What it is for

Phases A–D flag problems. Answering thousands of flags row by row in Excel, and running reshaping scripts by hand,
was the slow part. The agent replaces that manual step: "accept all date fixes except row 14", "Spend is in
thousands", "unpivot Day 1 to Day 31 into rows".

## How one instruction is handled

```
instruction ─▶ command parser (no model) ──understood──▶ typed operations
                     │ not understood
                     ▼
               local model: ONE call, JSON-schema-constrained, temperature 0
                     │ invalid answer ─▶ ONE repair call with the reason ─▶ still invalid ─▶ a question to the analyst
                     ▼
               typed operations ─▶ phase check (registry) ─▶ dry run ─▶ guardrails ─▶ stored proposal + preview
                                                                                          │
                                              analyst clicks Apply (ticks a confirmation when "elevated")
                                                                                          ▼
                                    apply job: same code as a review upload ─▶ rules re-check ─▶ snapshot ─▶ audit
```

| Step | Code | Notes |
|---|---|---|
| Tool registry | `src/agent/registry.py` | One `Tool` per operation: input model, phases, risk, row filter, summary. The model's prompt and schema, the phase limits and `mcp_tools()` are derived from it |
| Command parser | `src/agent/planner/parser.py`, `transform_commands.py` | Deterministic; works with no model |
| Model planner | `src/agent/planner/llm.py` | Prompt version `agent-plan/3`; at most 6 operations per plan; 2 attempts |
| Context | `src/agent/planner/context.py` | Column names, column kinds, up to 6 top values per text column, open finding groups, the last 6 turns. Never whole rows |
| Guardrails | `src/agent/guardrails.py` | Elevated = removes rows / columns / tables, aggregates, ≥ 1,000 cells, or a table losing > 5% of its rows |
| Executors | `src/agent/executors.py` (Phase B), `transform/engine.py` (Phase T, Polars) | |
| Use cases | `src/agent/service.py` | message, apply, undo, discard |
| Safe patterns | `src/agent/safe_regex.py` | 500-character limit, 0.25 s per match |

## Privacy

- **Local inference only.** The client refuses any model address that is not this machine or a host named in
  `DQ_LLM_ALLOWED_HOSTS` (`src/structure/llm/client.py::host_allowed`), before anything is sent.
- In the Compose deployment the model container sits on an **internal network with no route out**.
- The model receives column names and a few sample values, not tables. A stricter mode exists in code (not yet a setting) that drops the
  sample values and finding examples.
- Cell text reaches the model only as JSON string values inside a block labelled as data.
- Every call is recorded in `audit.db` (`llm_calls`): model, build digest, prompt version, timing, sizes, outcome.
  **The prompt and the answer are not stored.**
- A cloud model is not supported by configuration. Using one would mean sending column names and sample values to
  a third party; that needs an explicit decision and code change, never a silent setting.

## Human in the loop

| Control | Behaviour |
|---|---|
| Preview | Every plan is dry-run; nothing changes before Apply |
| Stale check | A proposal is refused if the data changed after its preview |
| Confirmation | Elevated changes are refused with `409 confirmation_required` unless `confirm: true`; the UI shows the reasons and a tick box |
| Undo | The last 10 applied changes can be undone |
| Phase acceptance | A button only; the agent has no tool for it |

## Evaluation

`evals/agent/cases.yaml`: 110 instructions in three tiers. Each ends as **correct**, **safe** (no change proposed
although one was expected) or **wrong** (a different change proposed). The plan is dry-run before scoring, as in the
product.

```bash
python -m src.agent.evaluation                       # the command parser alone (non-agentic baseline)
```

```bash
python -m src.agent.evaluation --llm on --model qwen2.5:7b-instruct --out report.json
```

Measured on 2026-11-12 (parser alone, `evals/agent/baseline_parser.json`):

| Tier | Cases | Correct | Safe | Wrong | Accuracy |
|---|---|---|---|---|---|
| command (documented wording) | 55 | 54 | 1 | 0 | 98.2% |
| paraphrase (own words) | 35 | 7 | 28 | 0 | 20.0% |
| adversarial (must not change) | 20 | 20 | 0 | 0 | 100% |

**The local model has not been measured.** The environment this was built in cannot reach Ollama's model registry.
The 28 "safe" paraphrase cases are exactly what the model is for; run the second command on the target hardware
for each candidate model and record the table. Release rule: accuracy must not fall, and `wrong` must be 0 on the
adversarial tier. `tests/test_agent_eval.py` enforces this for the parser on every commit.

## Choosing a model

| Model | Memory | Use |
|---|---|---|
| `llama3.2:3b` | ~4 GB, CPU is workable | Minimum; expect weaker paraphrase accuracy |
| `qwen2.5:7b-instruct` | ~8 GB, GPU recommended | Suggested starting point for the agent |
| `qwen2.5:14b-instruct` | ~16 GB GPU | If the 7B misses the target |

These are starting points, not results: decide with the evaluation. The model needs JSON-schema structured output
(Ollama's `format` parameter). Context is set to 16,384 tokens.

## Failure handling

| Failure | Result |
|---|---|
| Model unreachable or not pulled | The chat says so and lists the built-in commands |
| No answer / timeout (60 s per call) | "I did not understand", no change |
| Invalid answer twice | A question asking the analyst to rephrase |
| Tool of another phase | An explanation pointing to the right phase |
| Unknown column or table, ambiguous table | A question listing the real names |
| Pattern too slow | Refused with a hint to simplify it |

## Not built

Read-only investigation ("how many blank cities in March?"), clarifying choices as buttons, agent support for
Phases A, C and D, an MCP adapter (the registry exports the tool definitions for one), a SQL escape hatch.
