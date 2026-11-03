import type { IssuesView } from "../../types";

const STARTERS = [
  "show recipe",
  "list recipes",
  "save recipe as ",
];

/** Phase T's left pane: the recipe so far (each step with rows before -> after), and the output tables. */
export function RecipePanel({ issues, disabled, onSuggest }: { issues: IssuesView | null; disabled: boolean; onSuggest: (t: string) => void }) {
  if (!issues) return <p className="muted small" style={{ padding: 14 }}>Loading the recipe…</p>;
  const steps = issues.steps ?? [];
  return (
    <div className="agent-issues">
      <div className="agent-pane-head">
        <b>Recipe</b>
        <span className="muted small">{steps.length} step{steps.length === 1 ? "" : "s"}</span>
      </div>
      <div className="recipe-pane">
        {issues.failed && (
          <div className="alert error small"><div><b>Step {issues.failed.step} does not run</b>{issues.failed.message} Undo it, or ask for a step that fits.</div></div>
        )}
        {steps.length === 0 ? (
          <p className="muted small">No steps yet. The tables in the middle are the checked data. Ask for a change on the right, for example “unpivot Day 1 to Day 31 into rows”.</p>
        ) : (
          <ol className="recipe-list" aria-label="Recipe steps">
            {steps.map((s) => (
              <li key={s.step} title={s.notes.join(" ")}>
                <span>{s.text}</span>
                <span className="muted small">{s.summary}</span>
              </li>
            ))}
          </ol>
        )}
        <div>
          <b className="small">Output tables</b>
          <ul className="recipe-tables">
            {issues.tables.map((t) => <li key={t.ref}><span>{t.ref}</span> <span className="muted small">{(t.rows ?? 0).toLocaleString()} rows · {t.columns.length} cols</span></li>)}
          </ul>
        </div>
        <div className="row" style={{ gap: 6 }}>
          {STARTERS.map((s) => <button key={s} className="btn sm" disabled={disabled} onClick={() => onSuggest(s)}>{s.trim()}</button>)}
        </div>
      </div>
    </div>
  );
}
