import type { IssueGroup, IssuesView } from "../../types";
import { suggestionsFor } from "./logic";

export function groupKey(g: IssueGroup): string {
  return `${g.table}|${g.rule}|${g.column ?? ""}`;
}

/** Open review items grouped by rule and column. Selecting one filters the sheet; its chips fill the chat box. */
export function IssueList({ issues, selected, onSelect, onSuggest, disabled }: {
  issues: IssuesView | null;
  selected: string | null;
  onSelect: (g: IssueGroup | null) => void;
  onSuggest: (text: string) => void;
  disabled: boolean;
}) {
  if (!issues) return <p className="muted small" style={{ padding: 14 }}>Loading issues…</p>;
  return (
    <div className="agent-issues">
      <div className="agent-pane-head">
        <b>Open issues</b>
        <span className="muted small">{issues.open.toLocaleString()} open · {issues.resolved.toLocaleString()} answered</span>
      </div>
      {issues.groups.length === 0 ? (
        <div className="empty small">Nothing is open. Every review item has an answer.</div>
      ) : (
        <ul className="issue-list" aria-label="Open issues">
          {issues.groups.map((g) => {
            const key = groupKey(g);
            const on = selected === key;
            return (
              <li key={key} className={`issue ${on ? "on" : ""} sev-${g.severity}`}>
                <button className="issue-main" aria-pressed={on} onClick={() => onSelect(on ? null : g)} title={g.why}>
                  <span className="issue-top">
                    <code>{g.rule}</code>
                    <span className="issue-col">{g.column ?? "whole table"}</span>
                    <span className="chip">{g.count.toLocaleString()}</span>
                  </span>
                  <span className="issue-what">{g.what}</span>
                  {g.examples.length > 0 && <span className="issue-ex">e.g. {g.examples.join(", ")}</span>}
                </button>
                {on && (
                  <div className="issue-actions">
                    {suggestionsFor(g).map((s) => (
                      <button key={s.label} className="btn sm" disabled={disabled} onClick={() => onSuggest(s.text)}>{s.label}</button>
                    ))}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
