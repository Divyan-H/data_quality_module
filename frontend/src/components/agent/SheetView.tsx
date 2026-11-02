import { useEffect, useState } from "react";
import { api, ApiError } from "../../api";
import type { AgentTable, IssueGroup, PhaseKey, RowsPage, SheetRow } from "../../types";
import { show, tableLabel } from "./logic";

const PAGE = 100;

function cellTitle(row: SheetRow, col: string): string | undefined {
  const f = row.flagged[col];
  const c = row.changed[col];
  const lines = [];
  if (f) lines.push(`${f.rule} needs a decision: ${f.why ?? ""}${f.proposed != null ? ` (proposed: ${f.proposed})` : ""}`);
  if (c) lines.push(`${c.rule}: ${show(c.old)} → ${show(row.values[col])}${c.note ? `. ${c.note}` : ""}`);
  return lines.length ? lines.join("\n") : undefined;
}

/** The phase's data as the review workbook shows it: green = changed, yellow = waiting for a decision. */
export function SheetView({ runId, phase, tables, focus, version, onClearFocus }: {
  runId: string;
  phase: PhaseKey;
  tables: AgentTable[];
  focus: IssueGroup | null;
  version: string;
  onClearFocus: () => void;
}) {
  const [table, setTable] = useState<string | null>(tables[0]?.ref ?? null);
  const [only, setOnly] = useState<"all" | "changed" | "flagged">("all");
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<RowsPage | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!table || !tables.some((t) => t.ref === table)) setTable(tables[0]?.ref ?? null);
  }, [tables, table]);
  useEffect(() => {
    if (focus) {
      setTable(focus.table);
      setOnly("all");
    }
    setOffset(0);
  }, [focus]);

  useEffect(() => {
    if (!table) return;
    let live = true;
    api.agentRows(runId, phase, {
      table, offset, limit: PAGE, rule: focus?.rule, column: focus?.column ?? undefined,
      only: only === "all" ? undefined : only,
    }).then((p) => { if (live) { setPage(p); setError(null); } })
      .catch((e) => live && setError(e instanceof ApiError ? e.message : "Could not load the rows."));
    return () => { live = false; };
  }, [runId, phase, table, offset, only, focus, version]);

  if (!table) return <div className="empty">No tables.</div>;
  const total = page?.total ?? 0;
  const from = total ? offset + 1 : 0;
  const to = Math.min(offset + PAGE, total);
  return (
    <div className="agent-sheet">
      <div className="agent-pane-head">
        {tables.length > 1 ? (
          <select aria-label="Table" value={table} onChange={(e) => { setTable(e.target.value); setOffset(0); }}>
            {tables.map((t) => <option key={t.ref} value={t.ref}>{tableLabel(t)}</option>)}
          </select>
        ) : (
          <b>{tableLabel(tables[0])}</b>
        )}
        <div className="seg" role="group" aria-label="Rows to show">
          {(["all", "changed", "flagged"] as const).map((o) => (
            <button key={o} aria-pressed={only === o} onClick={() => { setOnly(o); setOffset(0); }}>
              {o === "all" ? "All rows" : o === "changed" ? "Changed" : "Waiting"}
            </button>
          ))}
        </div>
        {focus && (
          <span className="chip info">
            {focus.rule} · {focus.column ?? "table"}
            <button className="chip-x" aria-label="Show all issues" onClick={onClearFocus}>×</button>
          </span>
        )}
        <span className="grow" />
        <span className="muted small">{from.toLocaleString()}–{to.toLocaleString()} of {total.toLocaleString()}</span>
        <button className="btn sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Previous</button>
        <button className="btn sm" disabled={to >= total} onClick={() => setOffset(offset + PAGE)}>Next</button>
      </div>
      {error && <div className="alert error" style={{ margin: 10 }}>{error}</div>}
      {page && page.column_items.length > 0 && !focus && (
        <div className="muted small agent-colnotes">
          Column notes: {page.column_items.slice(0, 4).map((c) => `${c.rule} ${c.column ?? ""}`).join(" · ")}
          {page.column_items.length > 4 ? ` and ${page.column_items.length - 4} more` : ""}
        </div>
      )}
      <div className="sheet-wrap">
        <table className="sheet">
          <thead>
            <tr>
              <th className="rowhead">Row</th>
              {page?.columns.map((c) => <th key={c}>{c}</th>)}
            </tr>
          </thead>
          <tbody>
            {page?.rows.map((r) => (
              <tr key={r.row_id}>
                <th className="rowhead" title={r.row_id}>{r.excel_row ?? "+"}</th>
                {page.columns.map((c) => {
                  const cls = r.flagged[c] ? "flag" : r.changed[c] ? (r.changed[c].rule === "agent" || r.changed[c].rule === "analyst" ? "edit" : "fix") : "";
                  return (
                    <td key={c} className={cls} title={cellTitle(r, c)}>
                      {r.values[c] ?? <span className="null">∅</span>}
                    </td>
                  );
                })}
              </tr>
            ))}
          </tbody>
        </table>
        {page && page.rows.length === 0 && <div className="empty small">No rows match.</div>}
      </div>
      <div className="sheet-legend small muted">
        <span><i className="sw fix" /> fixed by a rule</span>
        <span><i className="sw edit" /> changed by you / the agent</span>
        <span><i className="sw flag" /> waiting for a decision</span>
        <span>Hover a cell for details.</span>
      </div>
    </div>
  );
}
