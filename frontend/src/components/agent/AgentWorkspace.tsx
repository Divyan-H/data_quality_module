import { useCallback, useEffect, useState } from "react";
import { api, ApiError } from "../../api";
import type { AgentState, IssueGroup, IssuesView, Phase, PhaseKey, Run } from "../../types";
import { Alert, DownloadLink } from "../ui";
import { ChatPanel } from "./ChatPanel";
import { groupKey, IssueList } from "./IssueList";
import { RecipePanel } from "./RecipePanel";
import { dataVersion } from "./logic";
import { SheetView } from "./SheetView";

/**
 * Fix with agent: the phase's review as a working screen. Left, the open issues; centre, the data as the review
 * workbook shows it; right, the chat. Every instruction comes back as a preview; Apply runs it as a job (the same
 * path as an uploaded review workbook), after which issues, rows and the session refresh.
 */
export function AgentWorkspace({ run, phase, nextKey, onClose, onAccept, onChange }: {
  run: Run;
  phase: Phase;
  nextKey: PhaseKey | null;
  onClose: () => void;
  onAccept: () => void;
  onChange: (r: Run) => void;
}) {
  const [state, setState] = useState<AgentState | null>(null);
  const [issues, setIssues] = useState<IssuesView | null>(null);
  const [focus, setFocus] = useState<IssueGroup | null>(null);
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const version = dataVersion(phase);
  const jobAction = run.job?.phase === phase.key ? run.job.action : null;
  const working = jobAction === "agent_apply" ? "Applying the change and re-checking the data…"
    : jobAction === "agent_undo" ? "Undoing the last change…" : null;
  const busy = sending || !!working || (run.job != null && run.job.phase !== phase.key);

  const fail = (e: unknown) => setError(e instanceof ApiError ? e.message : "Something went wrong.");

  const loadState = useCallback(() => {
    api.agent(run.id, phase.key).then(setState).catch(fail);
  }, [run.id, phase.key]);

  useEffect(() => {
    loadState();
    if (phase.status === "running") return;
    let live = true;
    api.agentIssues(run.id, phase.key).then((i) => {
      if (!live) return;
      setIssues(i);
      setFocus((f) => (f && i.groups.some((g) => groupKey(g) === groupKey(f)) ? f : null));
    }).catch(fail);
    return () => { live = false; };
  }, [run.id, phase.key, version, loadState, phase.status]);

  async function send(text: string) {
    setSending(true);
    setError(null);
    try {
      const r = await api.agentSay(run.id, phase.key, text);
      setState(r.state);
      setDraft("");
    } catch (e) {
      fail(e);
    } finally {
      setSending(false);
    }
  }

  async function apply(cs: string) {
    setError(null);
    try {
      // The card only enables Apply for an elevated change once the analyst has ticked the confirmation.
      const elevated = state?.changesets[cs]?.review?.level === "elevated";
      onChange(await api.agentApply(run.id, phase.key, cs, elevated));
    } catch (e) {
      fail(e);
      loadState();
    }
  }

  async function discard(cs: string) {
    try {
      setState(await api.agentDiscard(run.id, phase.key, cs));
    } catch (e) {
      fail(e);
    }
  }

  async function undo() {
    setError(null);
    try {
      onChange(await api.agentUndo(run.id, phase.key));
    } catch (e) {
      fail(e);
    }
  }

  const review = phase.artifacts.find((a) => a.primary);
  const transform = phase.key === "T";
  const accepted = phase.status === "accepted";
  const downstream = run.phases.some((p) => p.key > phase.key && p.status !== "locked" && p.status !== "ready");
  return (
    <div className="agent-ws">
      <div className="agent-top">
        <div>
          <h2>{transform ? "Transform with agent · Phase T" : `Fix with agent · Phase ${phase.key}`}</h2>
          <p className="muted small">
            {transform
              ? "Say how to reshape the data. Each step is previewed on the current tables; Apply adds it to the recipe, which runs again whenever the checked data changes."
              : "Say what to change. Each instruction is previewed; Apply makes the change, logs it in the audit and re-checks the data."}
            {downstream ? " Applying a change discards the later phases, which then run again." : ""}
          </p>
        </div>
        <div className="row">
          {review && <DownloadLink href={api.fileUrl(run.id, review.id)}>Download workbook</DownloadLink>}
          <button className="btn" onClick={onClose}>Back to review</button>
          <button className="btn primary" onClick={onAccept} disabled={busy || accepted || phase.status !== "review" || !phase.can_continue}>
            {accepted ? "Accepted" : nextKey ? `Accept and continue to Phase ${nextKey}` : "Accept and finish"}
          </button>
        </div>
      </div>
      {error && <Alert kind="error">{error}</Alert>}
      {phase.agent_error && !working && <Alert kind="warn" title="The last change was not applied">{phase.agent_error.message}</Alert>}
      <div className="agent-grid">
        {transform ? (
          <RecipePanel issues={issues} disabled={busy} onSuggest={(t) => setDraft(t)} />
        ) : (
          <IssueList
            issues={issues}
            selected={focus ? groupKey(focus) : null}
            onSelect={setFocus}
            onSuggest={(t) => setDraft(t)}
            disabled={busy}
          />
        )}
        {issues ? (
          <SheetView runId={run.id} phase={phase.key} tables={issues.tables} focus={focus} version={version} onClearFocus={() => setFocus(null)} plain={transform} />
        ) : (
          <div className="agent-sheet"><p className="muted small" style={{ padding: 14 }}>Loading the data…</p></div>
        )}
        <ChatPanel
          state={state}
          busy={busy}
          sending={sending}
          working={working}
          draft={draft}
          setDraft={setDraft}
          onSend={send}
          onApply={apply}
          onDiscard={discard}
          onUndo={undo}
          phase={phase.key}
        />
      </div>
    </div>
  );
}
