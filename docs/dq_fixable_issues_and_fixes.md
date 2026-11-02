# Data quality: which issues are fixable, what was fixed yesterday, and how

Written 2026-10-28. "Yesterday" is 2026-10-27. This note covers the issues raised sheet by sheet (Influencer, Coupon, Affiliate, Reactivation and the general ones) and says for each whether the data quality module can fix it, what is already done, and how.

"Fix" in data quality means: **find it, flag it with the old value and the reason, and change nothing without an answer.** Reshaping data (merging files, flattening a cross tab, extracting) stays in the transformation scripts.

---

## 1. At a glance

| # | Sheet | Issue | Fixable in data quality? | Status |
|---|---|---|---|---|
| 1 | Coupon | Several cities in one cell | Yes | **Fixed yesterday** (rule D-29) |
| 2 | All files | Negative values | Yes | **Fixed yesterday** (rule D-15) |
| 3 | All files | Choose which columns to check | Yes (needs the UI) | Next: UI and saved setup |
| 4 | DAP | Data changes after it was received | Yes | Next: overlap check on new data |

**Fixed yesterday: 2. Fixable next: 2.** Nothing on the list is unfixable. Item 3 needs a screen in the UI and item 4 needs a business rule from you (section 4).

---

## 2. What was fixed yesterday, and how

### 2.1 Several values in one cell (rule D-29), the "multiple cities" issue

**The problem we found.** A cell such as `Mumbai; Pune` in a text column was not flagged at all. In a numeric column, `300;400` was set to blank with the generic message "not a number". A cell like `12,34` was silently read as 12.34 and `100,200` as 100200, so two numbers could merge into one without a trace.

**What was built** (`multi_value_cells` in `src/hygiene/rules.py`, run before the date and number rules):

| Cell | What is flagged | Message shows |
|---|---|---|
| Text | A cell split by `;`, `\|`, `/`, `&` or a comma and a space into two or more parts that each have a letter and at least two characters | The separator found and the parts |
| Numeric | Two or more numbers in one cell (`300;400`, `1,200 , 1,500`) | "holds 2 numbers separated by ';'" |
| Numeric, ambiguous comma | `12,34` or `100,200` when no other cell in the column uses that comma style | The proposed reading and the alternative (two numbers) |
| Date | Several dates in one cell | The separator found |

**How it keeps its promise "never change without an answer":**
- The cell keeps its exact text. A hold list stops every later rule (number conversion, case merge, outlier cap, unit fix, date rules, gap filling) from touching it.
- A numeric column that still holds one such cell is written as text in the output, so nothing is converted quietly. When the item is answered, the column becomes a number column again.
- Single names with an ampersand (`R&D`, `AT&T`), `N/A`, `24/7` and amounts with a currency symbol or a comma pattern the column already uses (`$1,200`, `1,200`) are **not** flagged.
- A column where most cells are lists gets one flag for the column, not one per cell. At most 200 cells are listed per column, and the total is reported.

**How it is answered** (in `Needs_Review` of the review workbook):
- **Accept**: it is one value (for an ambiguous comma, take the proposed reading).
- **New Value**: type the one correct value.
- **Split** (text cells only): one row per value. Row IDs become `<id>:s1`, `<id>:s2` and the Excel row is kept, so every new row still points to its source row.

**Limit to know.** Split copies the row's other values onto each new row. It does not divide numbers. The audit says so on each split row.

### 2.2 Negative values (rule D-15)

**Before.** D-15 only flagged a cost or loss column that had *no* negative values. A negative spend, click or impression count went through unreported.

**Now** (`negative_values` in `src/hygiene/rules.py`). Every numeric column is checked. A column with at least one negative gets **one flag for the column**, for example:
`'Refunds' has 2 negative value(s) (2.2% of 90 numbers), e.g. -5, -12 at row 6, 16.`
Nothing is changed, because only the data provider can say whether a negative is valid (a refund, a credit, a return). Cells held by D-29 are not counted. Text columns are never checked.

### 2.3 A bug found while testing, and fixed

An end-to-end test on a new feature-check workbook showed that **D-27 (filling an isolated zero or blank) overwrote cells D-29 had flagged**: `300;400` became 1140 and `12,34` became 230. The gap filler read a held cell as "blank". Fixed in `src/hygiene/kpi.py`: a held cell is not a gap, and a gap is filled only when both neighbours are real numbers. A regression test covers it.

### 2.4 Two false alarms found by the full test run, and fixed

Running all tests after D-29 showed it flagged correct data: prices with a currency symbol (`€99,99`, `₹2,500`) and ordinary thousands figures (`1,200`, `12,500`). Now a currency symbol marks a cell as one formatted amount, and only a three-digit first part (`100,200`) is treated as ambiguous.

### 2.5 How it was checked

- **12 unit tests** for D-29 and the held-cell behaviour, plus tests for D-15.
- **A feature-check workbook**, `samples/e2e_feature_check.xlsx`, with a Plan sheet that lists what is planted in each sheet and what must happen. It runs through the real web flow in 4 tests: Phase B flags and review answers (New Value, Accept, Split) on daily data, Phase C on daily data, Phase C on monthly data turned into weeks, and Phase C findings on weekly data.
- **Full suite:** 518 passed. One test failed once on a Windows file lock when saving `state.json` and passes when run alone.
- All of it is committed and pushed (11 commits on `feature/analyst-web-app`).

---

## 3. What can still be fixed, and how

| # | Issue | How it would be fixed | Needs from you | Size |
|---|---|---|---|---|
| 3 | **Choose which columns to check in each file** | Put a column picker in the UI, and store the choice per client so the next upload starts pre-selected. Rules then run only on the chosen columns | The UI screen; where to save per client | Medium |
| 4 | **DAP: data changes after it was received** | On appending new data, compare it with what is already held on the same keys (date plus the label columns) and flag values that changed, as a numbered item with old and new value | The key columns, and the rule: new replaces old, old is kept, or ask each time | Medium to large |

## 4. What code cannot decide

- **Whether a negative value is valid.** D-15 only shows where they are. The data provider decides.
- **Which of two different values is right** when new data revises old data (item 4).

## 5. Suggested order

1. Item 3, the column picker, because it changes how every other rule is used.
2. Item 4 once the rule for new data that overlaps old data is agreed.
