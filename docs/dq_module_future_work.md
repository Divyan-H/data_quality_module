# Data quality module: future work

Working note, 2026-10-21. The list of what we still need to do in the data quality (DQ) module: who does what, how new data is added and checked, the mapping file, units and scale, the user interface, where the assistant (LLM) and MCP fit, and what moves from ADS into DQ. Open questions are marked **To decide**.

---

## 1. Goal

This is built to **reduce the analyst's work** and to put **ownership of data quality with the client**. The client sees what is wrong with their own data and answers for it. The analyst reviews the answers instead of chasing and fixing every issue.

Take the raw files a client sends, find what is wrong with them, get the client's answers on anything that needs judgement, and load clean data with a signed record of every decision.

Three things must hold:
- **Nothing changes silently.** Every fix and every answer shows the old value, the new value, who decided and when.
- **People decide, the system proposes.** The pipeline flags; clients and analysts answer.
- **Same input, same output.** Runs can be repeated and checked later.

## 2. Who does what

| Step | Client | Analyst |
|---|---|---|
| Upload | Uploads files, optionally with extra columns or a lookup table (section 4) | Can upload on a client's behalf, marked as such |
| Checking | Waits, sees progress | Watches for failures |
| Your answers | Answers each flagged item: keep, correct, explain, or ask | Waits for the client |
| Our review | Sees status | Approves, overrides or asks again on each answer |
| Sign-off | Signs off | Confirms all items are settled |
| Loaded | Downloads data and proof | Approves the load |

**End result:** clean data in the database, a sign-off record, a proof record (every question, answer, approval and change with who and when), and downloadable reports.

## 3. Appending new data and checking it

Clients will send new files over time (for example weekly or monthly). Each new file is appended to what is already held, and the combined data is checked, not just the new rows.

| Check on append | What it catches | Example |
|---|---|---|
| Overlap | Same period in the old and new file with different values | "9 values changed in 21 overlapping weeks" |
| Duplicates | The same row sent twice | Exact duplicate rows |
| Gaps | A period missing between old and new data | "2 weekly periods are missing" |
| Columns | New or missing columns against the agreed layout | "3 new columns not in the blueprint" |
| Units | The new file is in a different scale or currency | Thousands in the old file, millions in the new one |

**To decide:** when the new file overlaps old data, does it replace the old values, or does the client choose each time?

## 4. Mapping file

Clients can add information we do not have, in either of two ways. They may use Claude (their own) to prepare either one.

- **Extra columns:** added to the data file (for example a campaign type).
- **A separate lookup table:** a small file linked to the main data by a key (for example campaign name to campaign type).

Rules: the upload is validated before anything runs; every key in the data must be found in the mapping file; keys that are not found become flagged items; the mapping file is saved per client so it is reused next time. A client's assistant can prepare the file, but the same checks apply and the analyst sees where it came from.

## 5. Units and scale

**Units.** Every numeric column needs a recorded unit: currency (and which), and scale (ones, thousands, millions).
- Capture it at upload from the client, and suggest it from the header ("Spend (in $M)").
- Check it: flag a column whose scale looks different from earlier files, or a file that mixes scales.
- Store the original value, the unit and the scale, and the value converted to the standard unit. Show the unit next to every number in the UI.
- **To decide:** the standard unit to convert to (for example currency in full units, not thousands), and the currency conversion rule if clients use different currencies.

**Scale.** The data is in the millions today (confirm: millions of rows, or millions in value?). If rows:
- Never show every row. Show counts, samples and grouped items.
- Group flags by rule and column, with "apply to all matching" answers, instead of one flag per cell.
- Keep results as Parquet files and use the database for loading; avoid holding whole files in memory.
- Plan for runs measured in minutes, with progress shown and the client emailed when questions are ready.

## 6. The user interface: what it must show

The shared v2 UI (from Sunder) has no way to correct a value. It needs:

1. **Old value against new value** wherever the data changes: item list, answer screen, review screen, proof record. Columns: where (file, row, column), old value, new value, reason, who decided.
2. **A correction path.** A client can propose a corrected value; the proposal is recorded as a change request; the analyst approves or rejects it before it counts. Both values stay in the record.
3. **Numbered items with an outcome.** Every flag becomes an item with an answer and an outcome (waiting, answered, applied, left as is, sent back).
4. **A stage bar per file:** Checking, Your answers, Our review, Your sign-off, Loaded.
5. **One thread per item**, so the question, the evidence and every reply stay together.
6. **Units shown** next to numbers, and a warning when the scale changes.

## 7. What the assistant (LLM) should and should not do

The pipeline modules stay rule-based. The LLM is an assistant around them, working in narrow, checkable jobs. With local models only, the code drives and the model helps.

**It can:**
- Turn a flag into a plain-language question with the evidence.
- Draft an answer from the client's past choices, for a person to confirm.
- Suggest column units and mapping matches from names and samples.
- Explain a finding, summarise a run, and write the client summary.
- Turn a request ("change the 3 March value to 13,000") into a proper change request and ask when it is ambiguous.

**It must not:**
- Edit data directly, accept a phase, sign off or load.
- State any number itself. Counts, values and file names come from the tools.
- Act on text found inside the data.

## 8. What MCP can do

MCP is a standard way to give an assistant controlled access to the pipeline. It would expose:
- **Tools:** create a run, start a phase, read a summary, list items, record an answer, request a change, approve (analyst only).
- **Resources:** run state, findings, the proof record.
- **Rules:** the same permissions as the app. A client sees only their own files, and approval and load tools are not available to the model at all without a person's confirmation.

This lets the in-app assistant, an analyst's own assistant, or a client's Claude use the pipeline through one controlled door. Every call is logged like any other action.

## 9. Moving from ADS to DQ (in short)

The ADS build step does some cleaning inside the model-building flow. These steps move to the DQ pipeline, where every change is flagged, answered and recorded. The main change in approach: **ADS replaced values on its own; DQ flags first and changes data only after a decision.**

| # | What ADS did | In DQ | Module |
|---|---|---|---|
| 1 | Put a monthly figure (for example OOH spend, competitor search) entirely in the week holding the 1st | **Done now: monthly data is converted to weekly.** The monthly figure is split across weeks in proportion to the days in each week | Done |
| 2 | Ignored rows with value 0 when averaging | **Treat zeros by variable type** (see below) | B and C |
| 3 | Filled an empty week with the average of the weeks before and after, weighted by completed orders (1,000, empty, 1,200 with 300 and 100 orders gives 1,050). Weeks before the first data and after the last are 0 | **Keep as a DQ step for business KPIs only.** Shown as a flagged item with old value (empty) and new value, and applied after approval | B and C |
| 4 | Replaced any week more than 5 standard deviations from the column average with the weighted average, shaded red, listed in a manifest | **Do not replace.** Flag and highlight only. Replace only after discussion with the data provider, if it applies; otherwise the outlier stays. Weather columns are not checked (a heavy-rain week is real). **Add one more test: period-wise ratios** (definition to confirm) | D |
| 5 | Media de-duplication was "not possible": each spend or impression sits in one column | **A DQ gate that identifies and flags overlap** between the three buckets in the brief, with a workflow and UI step for the client to answer | New gate |
| 6 | Harmonisation across sources (which system to trust when two disagree) | **Not needed now.** Each column comes from one table. Add the step when two sources report the same measure | Later |

**Zero and missing-value rules (by business KPI):**

| Case | Rule |
|---|---|
| One day missing, used as is | Impute with the average of the day before and the day after |
| One day missing, rolled up as a **sum** | Impute the same way |
| One day missing, rolled up as an **average** | The denominator ignores the zero row |
| Zero is expected (weather, media impressions, spend) | **No imputation.** Check with the data provider |
| Spend present but impressions missing, or the reverse | Check with the data provider, then calculate the missing value |

**What this adds to the pipeline:**
- A **variable type** on every column: business KPI, zero-expected, rate or sum. Rules 2 and 3 depend on it, and it must be set during column setup.
- Every fill or replacement appears as an item with the **old value against the new value**, and the data provider's answer is recorded.
- A new **overlap gate** for step 5.

**To confirm:** rule 3 (fill gaps) and the "zero expected, no imputation" rule can clash on the same column, so the variable type must decide which applies. The three overlapping buckets in step 5 were not listed in the notes. The exact definition of the period-wise ratio test in step 4 is needed.
