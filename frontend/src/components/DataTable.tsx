import { useMemo, useState, type ReactNode } from "react";

export interface Column<T> {
  key: string;
  header: string;
  render?: (row: T) => ReactNode;
  className?: string;
  value?: (row: T) => string; // text used for searching
}

/** A small read-only table with search and an optional filter, for previews of findings. Not a spreadsheet. */
export function DataTable<T>({
  rows,
  columns,
  total,
  noun = "rows",
  filters,
  empty = "Nothing to show.",
}: {
  rows: T[];
  columns: Column<T>[];
  total?: number;
  noun?: string;
  filters?: { label: string; test: (row: T) => boolean }[];
  empty?: string;
}) {
  const [q, setQ] = useState("");
  const [filter, setFilter] = useState(0);
  const shown = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return rows.filter((r) => {
      if (filters && filter > 0 && !filters[filter - 1].test(r)) return false;
      if (!needle) return true;
      return columns.some((c) => (c.value ? c.value(r) : String((r as Record<string, unknown>)[c.key] ?? "")).toLowerCase().includes(needle));
    });
  }, [rows, columns, q, filter, filters]);

  if (rows.length === 0) return <p className="muted">{empty}</p>;
  return (
    <div>
      <div className="toolbar">
        <input type="text" placeholder={`Search ${noun}…`} value={q} onChange={(e) => setQ(e.target.value)} aria-label={`Search ${noun}`} style={{ minWidth: 220 }} />
        {filters && (
          <div className="seg" role="group" aria-label="Filter">
            <button aria-pressed={filter === 0} onClick={() => setFilter(0)}>All</button>
            {filters.map((f, i) => (
              <button key={f.label} aria-pressed={filter === i + 1} onClick={() => setFilter(i + 1)}>{f.label}</button>
            ))}
          </div>
        )}
        <span className="muted small">
          {shown.length.toLocaleString()} of {rows.length.toLocaleString()} shown
          {total !== undefined && total > rows.length ? ` · ${total.toLocaleString()} in total (open the workbook for all)` : ""}
        </span>
      </div>
      <div className="table-wrap">
        <table className="t">
          <thead>
            <tr>{columns.map((c) => <th key={c.key} className={c.className}>{c.header}</th>)}</tr>
          </thead>
          <tbody>
            {shown.slice(0, 500).map((r, i) => (
              <tr key={i}>
                {columns.map((c) => (
                  <td key={c.key} className={c.className}>
                    {c.render ? c.render(r) : String((r as Record<string, unknown>)[c.key] ?? "")}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {shown.length > 500 && <p className="muted small" style={{ marginTop: 6 }}>Showing the first 500 matches.</p>}
    </div>
  );
}
