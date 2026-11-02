# Spec DP-C — Time-Series / Execution Checks

> **Status:** Proposed · **Version:** 0.2 · **Series:** Data Pipeline phase spec C of 7 (A–G)
>
> **This is a new microservice — nothing below it exists yet.** `Data_Validation_Report_fixed.py`, the `data_quality_agent` repo, and the workflow document are reference material only, borrowed from where useful.
>
> **v0.2 correction.** v0.1 reported "5 real + 5 unreal" codes for this phase, based on a search that only covered `data_quality_agent-main.zip`. That was incomplete: `Data_Validation_Report_fixed.py` — a separate standalone script, not in that repo — is a complete, working implementation of **all 10** of this phase's codes (N-06–N-15). The workflow document itself already names this exact script as Phase C's real build (`dq_workflow.md`, Phase C section: "Built from: Data_Validation_Report_fixed.py"). This version replaces v0.1 entirely.

---

# 1. Scope

All 10 codes the workflow document assigns to this phase, all with real, grounded logic:

| Code | Description |
|---|---|
| N-06 | Execution weeks (count of valid, non-zero periods per variable) |
| N-07 | No Executions (9+ consecutive zero weeks) |
| N-08 | WOW Duplicate (week-over-week identical non-zero values) |
| N-09 | MOM Duplicate (month-over-month identical non-zero totals) |
| N-10 | Duplicate Variable Data (two variables with identical non-zero series) |
| N-11 | Duplicate Dates |
| N-12 | Missing Dates (weekly gaps) |
| N-13 | Date Order |
| N-14 | Weekly Continuity (all consecutive periods exactly 7 days apart) |
| N-15 | Duplicate Variable Names |

### 1.1 Terms
- **Variable** — one tracked metric column in the input file, scoped to the columns starting at the configured "Variables" position.
- **Checked vs. ignored variable** — a variable is only run through N-06–N-15 if it has a usable `Bucket` value in Variable Mapping; a blank, `0`, `#N/A`, `N/A`, `NA`, `NULL` or `NONE` bucket excludes it from checking entirely (it still appears in the input, just not validated).

---

# 2. Config

Shared with Phase D — same workbook, same sheets:

| Sheet | Relevant columns | Purpose |
|---|---|---|
| **Data File** | `Description`/`Column`, rows for `Period`, `Geography`, `Variables` | Maps input columns by letter reference |
| **Variable Mapping** | `Variables`, `Bucket` | Which variables to check (§1.1) |
| **Period Mapping** | `Period`, `Months` | Maps each date to a month label, for N-09's monthly rollup |
| **Data Check Specifications** *(optional)* | `Rule`, `From`, `To`, `No of Weeks` | Date-range restrictions for N-07/N-08/N-09; overrides the 9-week default for N-07 |

`Geography` is mapped and validated as a required config column but **not used by any check in this phase** — loaded and validated, never consulted. Worth deciding whether to keep requiring it in config if nothing reads it yet.

---

# 3. Processing

### 3.1 Numeric validity (feeds several checks, not a standalone flag)
A value is numeric-valid if it matches `^[+-]?(\d+(\.\d*)?|\.\d+)$` after whitespace trim. Non-numeric non-blank values, blanks, and explicit null tokens (`NULL`, `N/A`, `NA`, `NONE`, `NAN`, case-insensitive) are each their own error category — collected but not mutually exclusive within a variable's error list.

### 3.2 N-06 / N-07 — Execution weeks & no-executions runs
- **N-06** is a count, not a flag: number of periods with a numeric-valid, non-zero value. Always computed and shown per variable, error or not.
- **N-07**: walk the variable's periods in date order (optionally restricted to a configured `From`/`To` range); a run of ≥9 (configurable) consecutive periods that are each numeric-valid zero, with each consecutive pair exactly 7 days apart, is one occurrence. A gap that breaks 7-day continuity ends a run without counting it (a broken run isn't silently merged into the next one). Every occurrence is logged individually (start, end, week count, all period dates) — not just counted.

### 3.3 N-08 — WOW Duplicate
Within a variable's non-zero, dated values (optionally date-range restricted), group by identical value; for every pair of dates sharing a value, log the pair and label it `Sequential` (exactly 7 days apart) or `Not Sequential`. This is pairwise, not "first-seen vs. repeat" — a value appearing 3 times produces 3 pairs.

### 3.4 N-09 — MOM Duplicate
Aggregate a variable's non-zero values by calendar month (sum), then compare every pair of monthly totals for near-equality (`np.isclose`, tight tolerance) — not just the immediately preceding month. Same Sequential/Not Sequential labeling, based on whether the two months are calendar-adjacent.

### 3.5 N-10 — Duplicate Variable Data
For every pair of *checked* variables (§1.1), compare their full non-zero value series (position-aligned); an exact match flags both variables, cross-referencing each other by name.

### 3.6 N-11 – N-14 — Date integrity
Computed once, over the full distinct set of parsed period dates (not per-variable):
- **N-11** Duplicate Dates — any period value appearing more than once.
- **N-12** Missing Dates — any gap between two consecutive distinct dates greater than 7 days generates one synthetic "missing" date per missing week inside that gap.
- **N-13** Date Order — whether the raw period column is already monotonically increasing (before dedup/sort).
- **N-14** Weekly Continuity — whether every consecutive pair of *distinct, sorted* dates is exactly 7 days apart (this is different in scope from N-12: N-12 fills in what's missing, N-14 asserts nothing extra or irregular is present).

### 3.7 N-15 — Duplicate Variable Names
Computed from the **raw header row**, restricted to the configured Variables column range — not the whole sheet. This is intentionally narrower than a whole-sheet duplicate-header check (which belongs to the structural intake phase and covers every column regardless of purpose): this check only concerns itself with the variable-tracking columns this phase actually processes.

---

# 4. Rule Catalogue

| Code | Rule | Class | Detection | Config |
|---|---|---|---|---|
| N-06 | Execution weeks | Info | Count of numeric-valid non-zero periods | — |
| N-07 | No Executions | Flag | 9+ consecutive zero weeks, §3.2 | `min_weeks` (default 9), optional date range |
| N-08 | WOW Duplicate | Flag | Pairwise identical non-zero weekly values, §3.3 | optional date range |
| N-09 | MOM Duplicate | Flag | Pairwise identical non-zero monthly totals, §3.4 | optional date range |
| N-10 | Duplicate Variable Data | Flag | Identical non-zero series across two variables, §3.5 | — |
| N-11 | Duplicate Dates | Flag | Repeated period value | — |
| N-12 | Missing Dates | Flag | >7-day gap between distinct dates | — |
| N-13 | Date Order | Flag | Non-monotonic raw period column | — |
| N-14 | Weekly Continuity | Flag | Non-7-day gap between distinct dates | — |
| N-15 | Duplicate Variable Names | Flag | Repeated header text in the Variables column range | — |

Every rule is **flag-only** — this phase reports, it doesn't fix, matching the workflow document's own framing.

---

# 5. Outputs

`Data_Validation_Report.xlsx` — filename fixed by the reference script, matches the workflow document's own name for this phase exactly:

| Sheet | Content |
|---|---|
| `Overall_Summary` | PASS/FAIL for N-11–N-15 (dataset-wide checks) plus INFO rows (variables checked/ignored, total/first/last period) |
| `Errors` | One row per variable with any flag, all triggered checks concatenated, plus per-check period detail columns |
| `WOW_Duplicates` | Every N-08 pair |
| `MOM_Duplicates` | Every N-09 pair |
| `No_Executions` | Every N-07 occurrence, one row each |
| `Duplicate_Variable_Data` | Every N-10 pair |
| `Check_Index` | A plain-language description of every check — useful as the canonical user-facing wording for each rule |

---

# 6. Analyst Review: `Data_Validation_Report.xlsx`

Per the workflow document's own description (`dq_workflow.md`, Phase C): the Analyst downloads this report together with the data it checked, corrects data directly or marks a finding "Valid – no change," and re-uploads. Phase C re-runs against the corrected data; findings marked valid are not re-reported. This spec keeps that loop rather than inventing a separate review workbook shape — it's simpler than Phase B's Accept/Reject/New-Value model because corrections happen directly on the data sheet, not per-flag.

---

# 7. Open Questions

1. Should `Geography` stay a required config column given nothing in this phase reads it yet, or is it reserved for a near-term use (e.g., a future per-geography breakdown of these checks)?
2. N-10's full pairwise variable comparison is O(n²) in variable count — confirm this is acceptable at expected variable counts, or whether it needs a cheaper pre-filter (e.g., hash the non-zero series first, compare hashes before falling back to exact comparison).
3. Confirm N-15's scope (Variables column range only) is intentional and should stay separate from Structure's own S-03 header check, rather than being merged into one duplicate-header rule that runs once, early, over everything.

---
