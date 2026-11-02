import { number, plural } from "../format";
import { useEffect, useState } from "react";
import type { BucketRow, FindingRow, IssueRow, NotChecked, SummaryA, SummaryB, SummaryC, SummaryCTable, SummaryD, SummaryDTable } from "../types";
import { DataTable } from "./DataTable";
import { Alert, Stat } from "./ui";

/** Phase B items about a whole column have no row id. */
export const rowLabel = (id: string | null | undefined): string => (id ? (id.split(":").pop() ?? id) : "(column)");

const statusTone = (s: string) => (s === "Warning" ? "warn" : s === "Fixed" ? "ok" : "info");

/** Which of Phase A's tables go on to Phase B: tick boxes, saved with one click (no workbook round trip). */
function TableChoice({ s, onSave }: { s: SummaryA; onSave?: (refs: string[]) => Promise<void> }) {
  const saved = s.tables.filter((t) => t.selected).map((t) => t.ref);
  const [picked, setPicked] = useState<Set<string>>(new Set(saved));
  const [saving, setSaving] = useState(false);
  useEffect(() => setPicked(new Set(saved)), [saved.join("|")]); // eslint-disable-line react-hooks/exhaustive-deps
  const changed = picked.size !== saved.length || saved.some((r) => !picked.has(r));
  const toggle = (ref: string) => setPicked((p) => {
    const n = new Set(p);
    if (n.has(ref)) n.delete(ref);
    else n.add(ref);
    return n;
  });
  return (
    <section>
      <div className="row" style={{ justifyContent: "space-between", marginBottom: 8 }}>
        <h3 style={{ margin: 0 }}>Tables found</h3>
        {onSave && (
          <button className="btn primary sm" disabled={!changed || saving || picked.size === 0}
            onClick={() => { setSaving(true); onSave(s.tables.filter((t) => picked.has(t.ref)).map((t) => t.ref)).finally(() => setSaving(false)); }}>
            {saving ? "Saving…" : picked.size === 0 ? "Keep at least one table" : "Save table choice"}
          </button>
        )}
      </div>
      {onSave && <p className="muted small" style={{ margin: "0 0 8px" }}>Untick a table (a lookup or notes sheet, say) to leave it out of every later phase and the final files.</p>}
      <DataTable<SummaryA["tables"][number]>
        rows={s.tables}
        noun="tables"
        columns={[
          { key: "selected", header: "Keep", render: (r) => onSave
            ? <input type="checkbox" aria-label={`Keep ${r.sheet ?? r.ref}`} checked={picked.has(r.ref)} onChange={() => toggle(r.ref)} />
            : <span className={`chip ${r.selected ? "ok" : ""}`}>{r.selected ? "Yes" : "No"}</span>, value: (r) => (r.selected ? "yes" : "no") },
          { key: "file", header: "File" },
          { key: "sheet", header: "Sheet", render: (r) => r.sheet ?? "–" },
          { key: "range", header: "Range", render: (r) => r.range ?? "–" },
          { key: "rows", header: "Rows", className: "num", render: (r) => number(r.rows) },
          { key: "columns", header: "Cols", className: "num" },
          { key: "decided_by", header: "Header chosen by", render: (r) => r.decided_by ?? "–" },
        ]}
      />
    </section>
  );
}

export function SummaryAView({ s, onSelect }: { s: SummaryA; onSelect?: (refs: string[]) => Promise<void> }) {
  const warn = s.issue_counts.Warning ?? 0;
  return (
    <div className="stack">
      {s.blocking.length > 0 && (
        <Alert kind="error" title={`${plural(s.blocking.length, "blocking issue")}`}>
          <ul>{s.blocking.slice(0, 6).map((b, i) => <li key={i}>{b}</li>)}</ul>
        </Alert>
      )}
      <div className="stats">
        <Stat value={number(s.files)} label={s.files === 1 ? "file read" : "files read"} />
        <Stat value={number(s.selected_tables)} label={`of ${number(s.tables_found)} tables selected`} tone={s.selected_tables ? "ok" : "bad"} />
        <Stat value={number(s.rows_out)} label={`rows out (${number(s.rows_in)} in)`} />
        <Stat value={number(s.issue_counts.Fixed ?? 0)} label="structural fixes applied" tone="ok" />
        <Stat value={number(warn)} label="warnings to look at" tone={warn ? "warn" : undefined} />
      </div>
      {s.llm.mode !== "off" && (
        <Alert kind={s.llm.fallback ? "warn" : "info"} title={s.llm.fallback ? "The LLM review stopped early" : "Local LLM review"}>
          {(s.llm.questions ?? 0) === 0
            ? `${s.llm.model ?? "The model"} was available, but the rules were confident on every decision, so it was not asked anything.`
            : `${s.llm.questions} decision(s) were put to ${s.llm.model ?? "the model"} in ${s.llm.calls ?? 0} call(s): ${s.llm.agreed ?? 0} agreed with the rules, ${s.llm.changed ?? 0} changed, ${s.llm.overruled ?? 0} overruled by the safety checks.`}
          {s.llm.fallback ? " The remaining decisions were made by the rules." : ""}
        </Alert>
      )}
      <TableChoice s={s} onSave={onSelect} />
      <section>
        <h3 style={{ marginBottom: 8 }}>What was found and what was done</h3>
        <DataTable<IssueRow>
          rows={s.issues}
          total={s.issues_total}
          noun="issues"
          filters={[
            { label: "Warnings", test: (r) => r.Status === "Warning" },
            { label: "Fixed", test: (r) => r.Status === "Fixed" },
            { label: "Identified", test: (r) => r.Status === "Identified" },
          ]}
          empty="No structural issue was found."
          columns={[
            { key: "Status", header: "Status", render: (r) => <span className={`chip ${statusTone(r.Status)}`}>{r.Status}</span> },
            { key: "Issue", header: "Issue" },
            { key: "File", header: "File" },
            { key: "Where", header: "Where", render: (r) => [r.Sheet, r.Where].filter(Boolean).join(" · ") },
            { key: "What was found", header: "What was found", className: "wrap" },
            { key: "Action taken", header: "Action taken", className: "wrap" },
          ]}
        />
      </section>
    </div>
  );
}

/** D-28: columns (or labelled rows) that add up to another one - what to sum and what not to add together. */
function BucketsView({ buckets }: { buckets: BucketRow[] }) {
  return (
    <section>
      <h3 style={{ marginBottom: 8 }}>Columns that add up (double-counting risk)</h3>
      <p className="muted small" style={{ margin: "0 0 8px" }}>
        Found from the numbers, not the names. The next modules add everything up, so a total must not be added together with its parts.
        Accept or Reject each one on <code>Needs_Review</code> in the review workbook.
      </p>
      <DataTable<BucketRow>
        rows={buckets}
        noun="buckets"
        columns={[
          { key: "bucket", header: "Bucket", render: (b) => <b>{b.bucket}</b> },
          { key: "sheet", header: "Table", render: (b) => b.sheet ?? b.table_ref },
          { key: "where", header: "Found in", render: (b) => (b.where === "rows" ? `rows of ${b.label_column}` : "columns") },
          { key: "sum_these", header: "Add these", className: "wrap", render: (b) => b.sum_these.join(", ") || "–", value: (b) => b.sum_these.join(", ") },
          { key: "do_not_add_with_them", header: "Not together with them", className: "wrap", render: (b) => b.do_not_add_with_them.join(", ") || "–", value: (b) => b.do_not_add_with_them.join(", ") },
          { key: "status", header: "Your decision", render: (b) => (b.status === "open" ? "not decided" : b.status) },
        ]}
      />
    </section>
  );
}

export function SummaryBView({ s }: { s: SummaryB }) {
  return (
    <div className="stack">
      {s.unreadable.length > 0 && (
        <Alert kind="error" title="Some tables could not be read">
          <ul>{s.unreadable.map((u, i) => <li key={i}>{u.file}: {u.error}</li>)}</ul>
        </Alert>
      )}
      <div className="stats">
        <Stat value={number(s.tables.length)} label={s.tables.length === 1 ? "table cleaned" : "tables cleaned"} />
        <Stat value={number(s.totals.cells_fixed)} label="values fixed automatically" tone="ok" />
        <Stat value={number(s.totals.rows_dropped)} label="duplicate rows dropped" />
        <Stat value={number(s.totals.needs_review_open)} label="items need your decision" tone={s.totals.needs_review_open ? "warn" : undefined} />
        <Stat value={number(s.client_questions)} label="questions for the client" />
      </div>
      {s.tables.map((t) => (
        <p key={t.ref} className="muted">
          <b style={{ color: "var(--text)" }}>{t.file ?? t.ref}</b>
          {t.sheet ? ` · ${t.sheet}` : ""}: {t.summary}
        </p>
      ))}
      <section>
        <h3 style={{ marginBottom: 8 }}>Rules that fired</h3>
        <DataTable<SummaryB["by_rule"][number]>
          rows={s.by_rule}
          noun="rules"
          empty="No rule fired: nothing needed fixing or flagging."
          columns={[
            { key: "rule", header: "Rule", render: (r) => <code>{r.rule}</code> },
            { key: "what", header: "What it does", className: "wrap" },
            { key: "fixed", header: "Fixed", className: "num", render: (r) => number(r.fixed) },
            { key: "flagged", header: "Flagged", className: "num", render: (r) => number(r.flagged) },
          ]}
        />
      </section>
      {!!s.buckets?.length && <BucketsView buckets={s.buckets} />}
      {s.zero_expected && Object.keys(s.zero_expected).length > 0 && (
        <p className="muted small">
          Zero kept as a valid value (never filled): {Object.values(s.zero_expected).flat().join(", ")}.
        </p>
      )}
      <section>
        <h3 style={{ marginBottom: 8 }}>Needs your decision</h3>
        <DataTable<SummaryB["needs_review"][number]>
          rows={s.needs_review}
          total={s.needs_review_total}
          noun="items"
          empty="Nothing needs a decision."
          columns={[
            { key: "rule", header: "Rule", render: (r) => <code>{r.rule}</code> },
            { key: "row_id", header: "Row", render: (r) => <code>{rowLabel(r.row_id)}</code>, value: (r) => r.row_id ?? "" },
            { key: "column", header: "Column", render: (r) => r.column ?? "(whole row)" },
            { key: "original_value", header: "Original", render: (r) => <code>{r.original_value ?? r.current_value ?? "–"}</code>, value: (r) => r.original_value ?? r.current_value ?? "" },
            { key: "current_value", header: "Now", render: (r) => <code>{r.current_value ?? "–"}</code> },
            { key: "why_flagged", header: "Why", className: "wrap" },
          ]}
        />
      </section>
      {s.fixes_total > 0 && (
        <details>
          <summary>{number(s.fixes_total)} change(s) made automatically</summary>
          <div style={{ marginTop: 8 }}>
            <DataTable<SummaryB["fixes"][number]>
              rows={s.fixes}
              total={s.fixes_total}
              noun="changes"
              columns={[
                { key: "rule", header: "Rule", render: (r) => <code>{r.rule}</code> },
                { key: "row_id", header: "Row", render: (r) => <code>{rowLabel(r.row_id)}</code>, value: (r) => r.row_id ?? "" },
                { key: "column", header: "Column", render: (r) => r.column ?? "(whole row)" },
                { key: "old_value", header: "Was", render: (r) => <code>{r.old_value ?? "–"}</code> },
                { key: "new_value", header: "Now", render: (r) => <code>{r.new_value ?? "–"}</code> },
                { key: "note", header: "Reason", className: "wrap" },
              ]}
            />
          </div>
        </details>
      )}
    </div>
  );
}

function FindingsTable({ rows, byCode, total }: { rows: FindingRow[]; byCode: Record<string, number>; total?: number }) {
  const codes = Object.keys(byCode);
  return (
    <DataTable<FindingRow>
      rows={rows}
      total={total}
      noun="findings"
      empty="No finding."
      filters={codes.map((c) => ({ label: `${c} (${number(byCode[c])})`, test: (r: FindingRow) => r.code === c }))}
      columns={[
        { key: "code", header: "Code", render: (r) => <code>{r.code}</code> },
        { key: "check", header: "Check" },
        { key: "variables", header: "Variables", render: (r) => r.variables.join(", ") || "–", value: (r) => r.variables.join(" ") },
        { key: "periods", header: "Periods", render: (r) => r.periods.join(" · ") || "–", value: (r) => r.periods.join(" ") },
        { key: "reason", header: "Finding", className: "wrap" },
        { key: "client_report", header: "Client", render: (r) => (r.client_report ? <span className="chip info">Report</span> : "") },
      ]}
    />
  );
}

function NotCheckedNote({ items, phase }: { items: NotChecked[]; phase: "C" | "D" }) {
  if (!items.length) return null;
  return (
    <Alert kind="info" title={`${plural(items.length, "table")} not checked by Phase ${phase} (passed on unchanged)`}>
      <ul>{items.map((t) => <li key={t.ref}><b style={{ display: "inline" }}>{t.sheet ?? t.file ?? t.ref}</b>: {t.reason}</li>)}</ul>
    </Alert>
  );
}

function PerTable<T extends { table: string; sheet: string | null; source_file: string | null }>(
  { tables, render }: { tables: T[]; render: (t: T) => JSX.Element }) {
  if (tables.length === 1) return render(tables[0]);
  return (
    <div className="stack">
      {tables.map((t) => (
        <section key={t.table} className="card card-pad">
          <h3 style={{ marginTop: 0 }}>{t.sheet ?? t.source_file ?? t.table} <span className="muted small"><code>{t.table}</code></span></h3>
          {render(t)}
        </section>
      ))}
    </div>
  );
}

export function SummaryCView({ s }: { s: SummaryC }) {
  return (
    <div className="stack">
      <NotCheckedNote items={s.not_checked} phase="C" />
      <PerTable tables={s.tables} render={(t) => <SummaryCTableView s={t} />} />
    </div>
  );
}

export function SummaryDView({ s }: { s: SummaryD }) {
  return (
    <div className="stack">
      <NotCheckedNote items={s.not_checked} phase="D" />
      {!!s.weekly?.length && (
        <Alert kind="info" title="Monthly data was also spread over weeks">
          {s.weekly.map((w) => (
            <p key={w.table_ref} style={{ margin: "4px 0" }}>
              <b>{w.sheet ?? w.source_file ?? w.table_ref}</b>: {plural(w.rows_in, "monthly row")} became {plural(w.rows_out, "weekly row")} (Monday to Sunday;
              a week that crosses a month end is a separate row in each month). Each month adds back up to its monthly figure
              {w.inexact.length ? ` — except ${w.inexact.length} row(s), see Weekly_Allocation` : ""}. The monthly table is unchanged; the weekly one is a separate file.
            </p>
          ))}
        </Alert>
      )}
      <PerTable tables={s.tables} render={(t) => <SummaryDTableView s={t} />} />
    </div>
  );
}

function SummaryCTableView({ s }: { s: SummaryCTable }) {
  const fails = Object.entries(s.dataset_checks).filter(([, v]) => v === "FAIL");
  const total = Object.values(s.findings_by_code).reduce((a, b) => a + b, 0);
  return (
    <div className="stack">
      <div className="stats">
        <Stat value={number(s.rows)} label={`rows checked (${s.periods.first ?? "?"} → ${s.periods.last ?? "?"})`} />
        <Stat value={`${s.variables.checked}/${s.variables.in_range}`} label="variables checked" />
        <Stat value={number(s.totals.findings_open)} label="open findings" tone={s.totals.findings_open ? "warn" : "ok"} />
        <Stat value={number(s.totals.findings_valid)} label="marked valid" />
      </div>
      <section>
        <h3 style={{ marginBottom: 8 }}>Whole-table checks</h3>
        <div className="row">
          {Object.entries(s.dataset_checks).map(([code, v]) => (
            <span key={code} className={`chip ${v === "PASS" ? "ok" : "bad"}`}>{code} {v}</span>
          ))}
        </div>
        {fails.length > 0 && <p className="muted small" style={{ marginTop: 6 }}>Open the report for the rows behind each failed check.</p>}
      </section>
      <section>
        <h3 style={{ marginBottom: 8 }}>Findings preview</h3>
        <FindingsTable rows={s.findings} byCode={s.findings_by_code} total={total} />
      </section>
    </div>
  );
}

function SummaryDTableView({ s }: { s: SummaryDTable }) {
  const open = s.totals.findings_open ?? 0;
  const total = Object.values(s.findings_by_code).reduce((a, b) => a + b, 0);
  return (
    <div className="stack">
      <div className="stats">
        <Stat value={number(s.rows)} label="rows checked" />
        <Stat value={`${s.pairs.run}/${s.pairs.mapped}`} label="Spend/Support pairs checked" />
        <Stat value={number(open)} label="open findings" tone={open ? "warn" : "ok"} />
        <Stat value={number(s.inactive_rules.length)} label="rules not running" tone={s.inactive_rules.length ? "warn" : "ok"} />
      </div>
      {s.inactive_rules.length > 0 && (
        <Alert kind="warn" title="These rules did not run because no threshold was configured">
          <ul>{s.inactive_rules.map((r, i) => <li key={i}>{r}</li>)}</ul>
          <p className="small" style={{ marginTop: 6 }}>Fill the thresholds in the client config workbook and run Phase D again to include them.</p>
        </Alert>
      )}
      <section>
        <h3 style={{ marginBottom: 8 }}>Findings</h3>
        <FindingsTable rows={s.findings} byCode={s.findings_by_code} total={total} />
      </section>
    </div>
  );
}
