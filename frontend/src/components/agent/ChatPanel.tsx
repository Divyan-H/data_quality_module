import { useEffect, useRef, useState, type KeyboardEvent } from "react";
import type { AgentMessage, AgentState, ChangePreview, ChangeSetView, PhaseKey, TransformPreview } from "../../types";
import { show, STARTERS, statusLabel } from "./logic";

export function ProposalCard({ cs, busy, onApply, onDiscard, onAgain }: {
  cs: ChangeSetView;
  busy: boolean;
  onApply: () => void;
  onDiscard: () => void;
  onAgain: () => void;
}) {
  const st = statusLabel(cs);
  if ("kind" in cs.preview && cs.preview.kind === "transform") {
    return <TransformCard cs={cs} p={cs.preview} st={st} busy={busy} onApply={onApply} onDiscard={onDiscard} onAgain={onAgain} />;
  }
  const p = cs.preview as ChangePreview;
  const samples = p.operations.flatMap((o) => o.effect.samples).slice(0, 8);
  const unmatched = p.operations.flatMap((o) => o.effect.unmatched);
  const notes = p.operations.flatMap((o) => o.effect.notes);
  return (
    <div className={`proposal ${cs.status}`} aria-label={`Change ${cs.id}`}>
      <div className="proposal-head">
        <b>{cs.id}</b>
        <span className={`chip ${st.tone}`}>{st.text}</span>
        {cs.source === "llm" && <span className="chip" title="Planned by the local model">model</span>}
      </div>
      <ul className="proposal-ops">
        {p.operations.map((o, i) => (
          <li key={i}><span>{o.text}</span> <span className="muted">{o.summary}</span></li>
        ))}
      </ul>
      {samples.length > 0 && (
        <div className="proposal-samples">
          <table className="t compact">
            <thead><tr><th>Row</th><th>Column</th><th>Before</th><th>After</th></tr></thead>
            <tbody>
              {samples.map((s, i) => (
                <tr key={i}>
                  <td className="num">{s.excel_row ?? "—"}</td>
                  <td>{s.column ?? (s.what ?? "row")}</td>
                  <td className="old">{show(s.old)}</td>
                  <td className="new">{s.what === "row removed" ? "removed" : show(s.new)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {unmatched.length > 0 && <p className="small warn-text">Not found / not changed: {unmatched.slice(0, 8).map((u) => show(u)).join(", ")}{unmatched.length > 8 ? " …" : ""}</p>}
      {notes.map((n, i) => <p key={i} className="small muted">{n}</p>)}
      <p className="small muted">
        Total: {p.summary}
        {p.follow_up_fixes > 0 ? ` Phase B's rules will also tidy ${p.follow_up_fixes} value(s).` : ""}
        {` ${p.open_items_after} review item(s) would stay open.`}
      </p>
      {cs.status === "proposed" && <ApplyRow cs={cs} busy={busy} label="Apply" onApply={onApply} onDiscard={onDiscard} />}
      {cs.status === "stale" && (
        <div className="row">
          <span className="small muted">The data changed after this preview.</span>
          <button className="btn sm" onClick={onAgain} disabled={busy}>Preview again</button>
        </div>
      )}
      {cs.status === "failed" && cs.error && <p className="small bad-text">{cs.error}</p>}
    </div>
  );
}

/** Apply / Discard. A change the guardrails marked "elevated" shows why and needs a tick before Apply is enabled. */
export function ApplyRow({ cs, busy, label, onApply, onDiscard }: {
  cs: ChangeSetView; busy: boolean; label: string; onApply: () => void; onDiscard: () => void;
}) {
  const elevated = cs.review?.level === "elevated";
  const [checked, setChecked] = useState(false);
  return (
    <>
      {elevated && (
        <div className="confirm-box" role="group" aria-label="This change needs your confirmation">
          <b>Check this one carefully.</b>
          <ul>{cs.review!.reasons.map((r, i) => <li key={i}>{r}</li>)}</ul>
          <label className="check">
            <input type="checkbox" checked={checked} onChange={(e) => setChecked(e.target.checked)} disabled={busy} />
            I have checked the preview and want to apply it
          </label>
        </div>
      )}
      <div className="row">
        <button className="btn primary sm" onClick={onApply} disabled={busy || (elevated && !checked)}>{label}</button>
        <button className="btn sm" onClick={onDiscard} disabled={busy}>Discard</button>
      </div>
    </>
  );
}

/** A Phase T proposal: each step with rows before -> after per table, notes, and the first rows of the result. */
function TransformCard({ cs, p, st, busy, onApply, onDiscard, onAgain }: {
  cs: ChangeSetView; p: TransformPreview; st: ReturnType<typeof statusLabel>; busy: boolean;
  onApply: () => void; onDiscard: () => void; onAgain: () => void;
}) {
  const last = [...p.operations].reverse().find((o) => o.effect.sample)?.effect.sample ?? null;
  const notes = p.operations.flatMap((o) => o.effect.notes);
  const unmatched = p.operations.flatMap((o) => o.effect.unmatched);
  return (
    <div className={`proposal ${cs.status}`} aria-label={`Change ${cs.id}`}>
      <div className="proposal-head">
        <b>{cs.id}</b>
        <span className={`chip ${st.tone}`}>{st.text}</span>
        <span className="chip">{p.operations.length === 1 ? "1 step" : `${p.operations.length} steps`}</span>
      </div>
      <ol className="proposal-ops">
        {p.operations.map((o, i) => (
          <li key={i}><span>{o.text}</span> <span className="muted">{o.summary}</span></li>
        ))}
      </ol>
      {last && last.columns.length > 0 && (
        <div className="proposal-samples">
          <table className="t compact">
            <thead><tr>{last.columns.map((c) => <th key={c}>{c}</th>)}</tr></thead>
            <tbody>
              {last.rows.map((r, i) => <tr key={i}>{r.map((v, j) => <td key={j}>{show(v)}</td>)}</tr>)}
            </tbody>
          </table>
          <p className="small muted" style={{ padding: "4px 8px" }}>First rows of {last.table} after the step.</p>
        </div>
      )}
      {unmatched.length > 0 && <p className="small warn-text">Not found / not matched: {unmatched.slice(0, 8).map((u) => show(u)).join(", ")}{unmatched.length > 8 ? " …" : ""}</p>}
      {notes.map((n, i) => <p key={i} className="small muted">{n}</p>)}
      {cs.status === "proposed" && <ApplyRow cs={cs} busy={busy} label="Apply and add to the recipe" onApply={onApply} onDiscard={onDiscard} />}
      {cs.status === "stale" && (
        <div className="row">
          <span className="small muted">The tables changed after this preview.</span>
          <button className="btn sm" onClick={onAgain} disabled={busy}>Preview again</button>
        </div>
      )}
      {cs.status === "failed" && cs.error && <p className="small bad-text">{cs.error}</p>}
    </div>
  );
}

function Bubble({ m, state, busy, onApply, onDiscard, onAgain }: {
  m: AgentMessage;
  state: AgentState;
  busy: boolean;
  onApply: (cs: string) => void;
  onDiscard: (cs: string) => void;
  onAgain: (text: string) => void;
}) {
  const cs = m.kind === "proposal" && m.changeset ? state.changesets[m.changeset] : undefined;
  return (
    <div className={`msg ${m.role} ${m.kind}`}>
      {cs ? (
        <ProposalCard cs={cs} busy={busy} onApply={() => onApply(cs.id)} onDiscard={() => onDiscard(cs.id)} onAgain={() => onAgain(cs.instruction)} />
      ) : (
        <div className="msg-text">{m.text}</div>
      )}
    </div>
  );
}

export function ChatPanel({ state, busy, sending, working, draft, setDraft, onSend, onApply, onDiscard, onUndo, phase = "B" }: {
  state: AgentState | null;
  busy: boolean;
  sending: boolean;
  working: string | null;
  draft: string;
  setDraft: (s: string) => void;
  onSend: (text: string) => void;
  onApply: (cs: string) => void;
  onDiscard: (cs: string) => void;
  onUndo: () => void;
  phase?: PhaseKey;
}) {
  const end = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const [focused, setFocused] = useState(false);
  const count = state?.messages.length ?? 0;
  useEffect(() => {
    end.current?.scrollIntoView?.({ block: "end" });
  }, [count, sending, working]);
  useEffect(() => {
    if (draft && !focused) input.current?.focus();
  }, [draft]); // eslint-disable-line react-hooks/exhaustive-deps

  const send = () => {
    const t = draft.trim();
    if (t && !busy) onSend(t);
  };
  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  };

  return (
    <div className="agent-chat">
      <div className="agent-pane-head">
        <b>Agent</b>
        {state && (
          <span className={`chip ${state.llm.available ? "ok" : ""}`} title={state.llm.message}>
            {state.llm.available ? `Model · ${state.llm.model}` : "Commands only"}
          </span>
        )}
      </div>
      {state?.undo && (
        <div className="undo-bar">
          <span className="small">Last change: <b>{state.undo.id}</b> “{state.undo.instruction}”</span>
          <button className="btn sm" onClick={onUndo} disabled={busy}>Undo last change</button>
        </div>
      )}
      <div className="chat-log" aria-live="polite">
        {count === 0 && (
          <div className="msg agent answer">
            <div className="msg-text">
              {phase === "T" ? (
                <>
                  Tell me how to reshape the tables, one step at a time. I show the result first; Apply adds the step to the recipe.
                  {"\n"}Try: “unpivot Day 1 to Day 31 into rows as Day and Value”, “split City into rows sharing Usage”,
                  “sum Impressions, Clicks by day, channel, network”, “look up CURRENT_AOV from AOV matching Date, City”,
                  “net next-month negatives in Spend by Month per Site”. Say “help” for the full list.
                </>
              ) : (
                <>
                  Tell me what to change, one instruction at a time. I show a preview first; nothing changes until you click Apply.
                  {"\n"}Try: “accept all date fixes except row 14”, “Head Office is Bangalore”, “Spend is in thousands”,
                  “add column Source with value Client”. Pick an issue on the left for ready-made answers.
                </>
              )}
            </div>
          </div>
        )}
        {state?.messages.map((m) => (
          <Bubble key={m.id} m={m} state={state} busy={busy} onApply={onApply} onDiscard={onDiscard} onAgain={onSend} />
        ))}
        {sending && <div className="msg agent"><div className="msg-text muted">Working out the change…</div></div>}
        {working && <div className="msg agent"><div className="msg-text muted">{working}</div></div>}
        <div ref={end} />
      </div>
      <div className="chat-input">
        <div className="row" style={{ gap: 6 }}>
          {STARTERS.map((s) => <button key={s} className="btn sm ghost" disabled={busy} onClick={() => onSend(s)}>{s}</button>)}
        </div>
        <textarea
          ref={input}
          aria-label="Instruction for the agent"
          placeholder={phase === "T" ? "e.g. sum Clicks, Installs by day, channel" : "e.g. set Region to APAC where Country is India"}
          value={draft}
          rows={2}
          maxLength={2000}
          onFocus={() => setFocused(true)}
          onBlur={() => setFocused(false)}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={onKey}
          disabled={busy && !sending}
        />
        <div className="row" style={{ justifyContent: "space-between" }}>
          <span className="muted small">Enter to send · Shift+Enter for a new line</span>
          <button className="btn primary sm" onClick={send} disabled={busy || !draft.trim()}>Send</button>
        </div>
      </div>
    </div>
  );
}
