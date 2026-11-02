# Spec DP-D — Trend & Period-over-Period Change

> **Status:** Proposed · **Version:** 0.3 · **Series:** Data Pipeline phase spec D of 7 (A–G)
>
> **This is a new microservice — nothing below it exists yet.** `Spend_Support_Trend_Report.py` and the workflow document are reference material, borrowed from where useful.
>
> **v0.3 changes**, resolving v0.2's open questions:
> 1. **New codes assigned.** The three checks v0.2 found without workflow-document codes now have real ones — **N-18, N-19, N-20** (§1). This extends the master code catalogue; see the updated workflow document delivered alongside this spec.
> 2. **The derived-ratio check (N-18) is generalized.** Not CPM-only — this phase now supports an arbitrary Spend/Support-style ratio per client (§3.3).
> 3. **No default thresholds anywhere in this phase.** Every rule in §4 requires an explicit config value; a rule with no configured threshold simply does not run. (§3.6)
> 4. **YoY/HoH-style comparison skip-on-no-history confirmed** — kept exactly as the reference script does it (§3.3, last item).

---

# 1. Scope

| Code | Description | Class |
|---|---|---|
| N-16 | Abnormal trend identifier | Flag |
| N-17 | YOY / QOQ / MOM / HOH % change threshold | Flag |
| **N-18** *(new)* | Derived-ratio % change threshold (generalized CPM-style check) | Flag |
| **N-19** *(new)* | Spend/Support availability mismatch | Flag |
| **N-20** *(new)* | Spend concentration ("Spend Skewness") | Flag |

**Not the same as D-14 (the cell-hygiene phase's own outlier flag).** N-16 is per-variable and longitudinal (a variable's own history over time); D-14 is per-column and cross-sectional (values within one column at one point in time). Same statistical method (median/MAD robust Z-score), different axis — not a duplicate.

### 1.1 Terms
- **Support variable / Spend variable** — a performance metric paired 1:1 with its spend metric via config.
- **Ratio metric** — any Spend/Support-style division a client wants tracked (CPM is the default example; CPA, CPC, or a client-specific ratio are equally valid — §3.3).
- **Bucket / Aggregation 1** — grouping labels used to roll up N-20's spend concentration.
- **Trend Line date axis** — the explicit date list from config's Period Mapping sheet; a configured date with no observation still appears, as zero.

---

# 2. Config

| Sheet | Columns | Purpose |
|---|---|---|
| **Period Mapping** | `Period`, `Months`, `Quarters`, `Half Years`, `Full Years` | Trend axis + period-grouping labels for N-17 |
| **Variable Mapping** | `Variables`, `Spend Variables`, `Y/N`, `Description`, `Bucket`, `Aggregation 1` | Support/Spend pairing; only `Y` rows run |
| **Data Check Specifications** | `Rule`, `Threshold` | One row per required threshold — see §4's Config column for exact keys, all mandatory (§3.6) |
| **Ratio Definitions** *(new, §3.3)* | `Ratio Name`, `Numerator`, `Denominator`, `Threshold` | One row per client-defined ratio metric to check under N-18 |

---

# 3. Processing

### 3.1 Pairing & axis
Unchanged from v0.2: for every `Y`-flagged Variable Mapping row, aggregate Support/Spend values onto the configured Trend Line dates, left-joined so unconfigured dates are dropped and configured-but-absent dates show as zero.

### 3.2 N-16 — Abnormal trend (robust Z-score per stream)
Unchanged from v0.2: median/MAD robust Z-score per Support and Spend stream independently, excluding zero/null, requiring ≥5 values. `mad == 0` fallback flags any deviation from the median directly (stricter than Hygiene's D-14 IQR fallback). Threshold is `Abnormal Trend — Robust Z-Score` in Data Check Specifications — **mandatory per §3.6**; if unset, N-16 does not run for that client.

### 3.3 N-17 & N-18 — Period-over-period % change, including arbitrary ratios
**N-17** (unchanged from v0.2): for MOM/QOQ/HOH/YOY independently, aggregate Support and Spend by period, compute sequential change and same-period-last-year change; flag if any of the four values meets its period type's threshold.

**N-18 (generalized, replacing v0.2's CPM-only treatment):** driven by the new `Ratio Definitions` config sheet, not hardcoded to Spend/Support:
1. For each row in `Ratio Definitions`: `ratio = Numerator / Denominator` (both must be mapped variables — either raw Support/Spend variables or another already-computed ratio is out of scope for v1, to avoid chained-ratio complexity).
2. Compute the ratio's own sequential % change per period type, same formula as N-17.
3. Flag using that row's own `Threshold` — independent of N-17's thresholds, since a ratio's normal volatility is usually different from its inputs' (e.g. CPM might reasonably swing 15% week to week while Spend itself swings 40%).

A client with no `Ratio Definitions` rows simply gets no N-18 checks — this is opt-in per client, not a default CPM check every client inherits.

**Same-period-last-year skip on insufficient history (both N-17 and N-18):** if the equivalent prior-year (or prior-half, for HOH) period doesn't exist in the data, that comparison is simply omitted for that row — not treated as a failure, not defaulted to zero. A client's first year of data will have no YOY column populated at all; this is expected, not an error state.

### 3.4 N-19 — Availability mismatch
Unchanged from v0.2: for a pair with both Support and Spend mapped, flag any date where exactly one of the two has a non-zero value (XOR). Always on when a pair has both variables mapped — no separate threshold needed, since this is a presence check, not a magnitude check.

### 3.5 N-20 — Spend concentration ("Spend Skewness")
Unchanged from v0.2: per `Bucket` + `Aggregation 1` group, per period (plus one `OVERALL`), rank Spend variables by total spend, flag if the top N's combined share exceeds the configured threshold. Two non-alert guard states kept distinct from `PASS`: `NOT ASSESSED — ZERO TOTAL SPEND`, `NOT ASSESSED — INSUFFICIENT TACTICS`.

### 3.6 Mandatory configuration (resolves v0.2's open question 3)
**No rule in this phase ships with a fallback default.** Every threshold — N-16's Z-score, each of N-17's four period thresholds, every N-18 ratio's threshold, N-20's top-N count and concentration percentage — must be explicitly set per client before that rule runs. A client onboarding without a fully configured `Data Check Specifications` (and, if using N-18, `Ratio Definitions`) sheet gets a smaller effective rule set, not a rule running on borrowed defaults. This phase's own config-upload check (whatever validates the client's config workbook before a run) must surface which of this phase's rules are currently inactive due to a missing threshold, so this is visible rather than a silent gap — e.g. as a line in `Rules_Used` (§5) listing every rule with no threshold configured.

---

# 4. Rule Catalogue

| Code | Rule | Class | Detection | Config (all mandatory, §3.6) |
|---|---|---|---|---|
| N-16 | Abnormal value in a variable's own time series | Flag | Robust Z-score vs. stream median/MAD, §3.2 | `robust_z_threshold` |
| N-17 | Period-over-period change exceeds threshold | Flag | 4 period types × 2 comparisons, §3.3 | `mom_pct`, `qoq_pct`, `hoh_pct`, `yoy_pct` |
| N-18 | Client-defined ratio's period-over-period change exceeds threshold | Flag | Per `Ratio Definitions` row, §3.3 | one `threshold` per ratio row |
| N-19 | Spend/Support availability mismatch | Flag | XOR presence check, §3.4 | — (on whenever both are mapped) |
| N-20 | Spend concentrated in few tactics | Flag | Top-N share vs. threshold, §3.5 | `top_n`, `concentration_pct` |

---

# 5. Outputs

`Spend_Support_Trend_Report.xlsx` — unchanged sheet set from v0.2, with one addition:

| Sheet | Content |
|---|---|
| `Trend Line` | Support/Spend values against the configured date axis |
| `MOM_Changes`, `QOQ_Changes`, `HOH_Changes`, `YOY_Changes` | N-17 detail |
| `Ratio_Changes` *(new)* | N-18 detail, one row per ratio per period, replacing v0.2's CPM-only columns folded into the change sheets |
| `Spend_Support_Availability` | N-19 detail |
| `Outlier_Details` | N-16 detail |
| `Spend Skewness` | N-20 alert rows |
| `Spend_Concentration` (hidden) | N-20 full ranked detail |
| `Rules_Used` (hidden) | Every threshold actually configured — and, per §3.6, this should also list which rules are **inactive** for lack of one |
| `Unmapped_or_Missing` | Support variables with no match in the input file |

---

# 6. Design Notes for This Microservice

1. Native-Excel pivot/chart building (`win32com`) from the reference script cannot run server-side — needs a real redesign (pre-aggregated data to a frontend, or a computed static pivot), not a port. (Unchanged from v0.2.)
2. §3.6's "which rules are currently inactive" surfacing is new in v0.3 — recommend a consistent, service-wide pattern for reporting inactive-due-to-missing-config rules, reused across every phase that has configurable thresholds, rather than each phase inventing its own.

---

# 7. Open Questions

1. N-18's `Ratio Definitions` sheet — does a client ever need a ratio of two *other* ratios (chained), or is numerator/denominator always a raw mapped variable? v0.3 assumes the latter (§3.3) to avoid unbounded chaining complexity; confirm before build.
2. Should N-19 (availability) support a magnitude threshold (e.g. "flag only if the missing side would plausibly have been >X") rather than any non-zero/zero mismatch, to cut down noise on genuinely small values?
3. Should the Data Check Specifications sheet reject an upload that's missing a rule's threshold outright (hard requirement), or accept it and simply run fewer rules (soft requirement, as this version assumes)? §3.6 assumes soft — confirm that's the intended strictness.

---
