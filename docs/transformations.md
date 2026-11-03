# Phase T — Transformations

**Status:** built, Nov 2026. Replaces the hand-run Porter scripts (`docs/Porter_Data_Transformation_v2.docx`) with typed,
previewed, recorded steps. Design background: `docs/agent_architecture.md` §4.5.

## Where it sits

```
A Structure -> B Hygiene -> C Time series -> D Trends -> T Transformations (optional) -> final files
```

- **Why after the checks.** Reshaping changes a table's grain (one row becomes several, columns become rows, rows are
  added up). Phases B–D check the data at the grain the client sent it, so their findings stay meaningful; the decision
  log already keeps sorting and reshaping out of Phase B.
- **Optional.** If the checked tables are already in the shape you load, click *Skip*. The run is complete when Phase D is
  accepted and T is either untouched, skipped or accepted.
- **Input.** Exactly the tables the final files would hold without T (the last checking phase's output, per table),
  named as the final files name them (sheet name, made unique).
- **Output.** The tables as the recipe leaves them. They become the final files (csv, parquet, xlsx, lineage).
  `Transform_Report.xlsx` lists every step with rows before → after, notes, and the first rows of each output table.

## The recipe

A recipe is an ordered list of typed steps, kept in `<run>/config/T_recipe.json`.

- **Built in the chat** (*Transform with agent*): each instruction is previewed on the current tables (rows before → after,
  new and removed columns, notes, and the first rows of the result). **Apply** runs the step, adds it to the recipe and
  saves the new tables. **Undo last change** restores the tables *and* the recipe.
- **Replayed.** When an earlier phase changes (a re-run, an uploaded review), T is cleared but its recipe is kept. Running
  T again replays every step on the new data. If a step no longer fits (a column has gone), the replay stops there,
  the report says which step and why, and *Accept* is blocked until the step is undone or replaced.
- **Saved and reused.** `save recipe as Coupons monthly` writes it to the library (`<workspace>/../recipes/`), shared by
  every run; on next month's run, `use recipe Coupons monthly` proposes all its steps as one previewed change.
  `list recipes` and `show recipe` do what they say.

## Operations

Every step is one of these typed operations (`src/agent/operations.py`), run by `src/agent/transform/engine.py`.
The chat understands the wording shown; the local model handles other wording and must produce the same operations.

| Operation | Say | Does |
|---|---|---|
| `unpivot` | `unpivot Day 1 to Day 31 into rows as Day and Reactivated` · `turn the Day columns into rows` | Columns become rows (wide → long); other columns repeat |
| `pivot` | `pivot Bucket into columns using Reactivated by Date, City (sum)` | Values become columns (long → wide), aggregated |
| `split_rows` | `split City into rows sharing Usage keeping Delhi NCR together` | One row per list item; numbers shared: equal_integer keeps the total (10 → 4, 3, 3), `equally` divides, `repeating` copies |
| `split_column` | `split Full Name into First and Last [on comma]` | One column into several (also in Phase B) |
| `allocate_by_share` | `allocate Spend, Reach by City from Audience using Pct matching Campaign [as whole numbers]` | Shares each row's numbers over the matching share rows; % or fractions; unallocated rest on a blank-category row |
| `aggregate` | `sum Impressions, Clicks by day, channel, network` · `count rows by Date, City as orders` · `average AOV by City` | Groups and adds up (blanks count as 0) |
| `bucket_others` | `label channel not in Channels as Others also network then sum clicks, installs by day` · `keep top 5 Channel, others as Others` | Relabels values off a list (or outside the top N), optionally adds them up |
| `net_next_period` | `net next-month negatives in Spend by Month per Site, Format` | OOH rule: a negative in the exact next month nets this month's allocation (Allocation, Next Month Return, Final Spend) |
| `append_rows` | `append DAP New to DAP Base where DATE >= 2025-09-09 renaming DATE as Date, CITY_NAME as City` | Stacks a table under another; columns matched by name |
| `lookup_join` | `look up CURRENT_AOV from AOV matching Date = ORDER_DATE, City = CITY_NAME in Orders` | Brings columns in where keys match (case, spaces and date formats ignored); duplicate keys averaged |
| `convert_currency` | `convert Spend from USD to INR at 83.2` · `… at 86.5 for Q1 2025, 85 for Q2 2025 using Date` | One rate, or rates by date range |
| `sort_rows` | `sort by Period, Geography` · `sort Coupons by Date descending` | Dates and numbers sort as dates and numbers |
| `make_date` | `make Date from Month and Day` | `Jan-25` + `Day 12` → 2025-01-12 (impossible dates left empty) |
| `make_period` | `add Month as the month of Order Date` | First day of the month / quarter / year / week |
| `classify` | `set Tier to Gold if Spend > 100000, Silver if Spend > 50000, else Bronze` | If / else rules (also in Phase B) |
| `drop_columns`, `drop_table` | `drop columns A, B` · `drop table AOV` | Leave out of the final data |
| value steps | `Head Office is Bangalore`, `remove '$' from Spend`, `add column Revenue = Orders * AOV`, `remove rows where …`, `rename column …` | The Phase B value operations work in T too |

A step that names no table goes to the one table that has every column it uses; with several candidates the chat asks.

## The Porter scripts, step by step

Each row is reproduced by a test in `tests/test_transform_engine.py` using the document's own worked example.

| Porter script | Recipe | Test |
|---|---|---|
| Coupon city split (2.2, 2.3) | `split City into rows sharing Usage keeping Delhi NCR together` | 10 over 3 cities → 4, 3, 3; NULL usage stays NULL |
| Coupon classification (2.4) | `look up Discount Type from Promo Map matching Promo = Promo Name` then `set Vehicle Segment to HCV if Desc contains truck, else All` | lookup + rules |
| Influencer city allocation (2.1) | `allocate Spend, Reach by City from Audience using Pct matching Campaign` | Spend 10,000 × 60% → Mumbai 6,000; the unallocated 10% on a blank-City row |
| Affiliate cumulater (2.5) | `sum impressions, clicks, installs by day, channel, network` | 100 + 50 → 150 |
| Affiliate channel Others (2.6) | `label channel not in Channels as Others also network then sum impressions, clicks, installs by day` | 10 + 20 → one Others row with 30 |
| Affiliate city split (2.7) | `allocate impressions, clicks by CITY from Cities matching day = DATE, network = NETWORK_NAME keeping unmatched cities` | 90 → 30, 30, 30; an unmatched city row kept with 0 |
| Reactivation crosstab (2.8) | `unpivot Day 1 to Day 31 into rows as Day and Reactivated` → `make Date from Month and Day` → `remove rows where Date is empty` → `pivot Inactivity Bucket into columns using Reactivated by Date, City, Accumulated Base` | 31 + 28 dated rows; bucket columns |
| OOH netting (2.9) | `net next-month negatives in Spend by Month per Site` | Oct-24 801,220 − 801,220 → 0; Nov-24 2,506,320 kept; Dec→Jan works |
| DAP consolidation (2.10) | `add column City from Geo Region ID: 1 = Mumbai, 2 = Delhi, …, others = unspecified` then `append DAP New to DAP Base where DATE >= 2025-09-09 renaming DATE as Date, CITY_NAME as City, VEHICLE_CATEGORY as Level 0 Mapping` | base order, then only newer rows; Geo ID blank on appended rows |
| Revenue from AOV (2.11) | `count rows by ORDER_DATE, ORDER_CITY, VEHICLE, SEG as orders` → `look up CURRENT_AOV from AOV matching …` → `add column revenue_amount = orders * CURRENT_AOV` | 2 × mean(200, 300) = 500; no AOV → blank |
| USD → INR (3) | `convert Spend from USD to INR at 86.5 for Q1 2025, 85 for Q2 2025 using Date` | rows outside the ranges left unconverted and counted |
| Two-sheet merge (3) | `append W2023 to W2022` then `sort by Period, Geography` | common columns; extra columns noted |
| Head Office → Bangalore, dollar removal, city names (3) | `Head Office is Bangalore`, `remove '$' from Spend` (Phase B or T) | value steps |

Not taken over: `platform_finder`, `product_type_finder`, `populate_competitor_search` and `extract_source_metadata` are
mapping/discovery utilities for Phase E (source-to-target mapping), not transformations of a run's data.

## Code map

| Module | Role |
|---|---|
| `src/agent/operations.py` | The typed operations (value and shape) |
| `src/agent/transform/engine.py` | Every operation on Polars frames; row lineage (`<id>:u3` unpivoted, `:s2` split, `:a1` allocated, `g<n>` grouped) |
| `src/agent/transform/workspace.py` | Recipe replay, preview, apply, undo snapshots, report, recipe library, read-only views |
| `src/agent/planner/transform_commands.py` | The plain-English commands above |
| `src/orchestration/phases/transform.py` | The phase adapter (optional, agent-enabled, no review upload) |
| `src/orchestration/sources.py` | Which parquet holds each final table (checked or transformed) |
| `frontend/src/components/agent/RecipePanel.tsx` | The recipe pane of the Transform-with-agent screen |

Tests: `tests/test_transform_engine.py` (Porter examples), `tests/test_transform_api.py` (end to end through the API:
optional skip, build/undo/accept, final files, replay after an upstream re-run, recipe reuse, a step that stops fitting).
