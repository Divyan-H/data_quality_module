import { useEffect, useState } from "react";
import { api, ApiError, type StartOptions } from "../api";
import { LlmDialog } from "../components/LlmDialog";
import { ConfigPanel, ReviewPanel, SkippedPanel, StartPanel } from "../components/panels";
import { RunningPanel } from "../components/RunningPanel";
import { Stepper, type View } from "../components/Stepper";
import { Alert, Modal } from "../components/ui";
import { navigate, useEvents, useRun } from "../hooks";
import type { LlmStatus, Phase, PhaseKey, Run, ServiceOptions } from "../types";
import { PHASE_KEYS } from "../types";
import { ResultsPage } from "./ResultsPage";

/** Where the analyst most likely wants to be: the first phase that is not accepted and not waiting. */
export function defaultView(run: Run): View {
  if (run.complete) return "results";
  const open = run.phases.find((p) => p.status !== "accepted");
  return open ? open.key : "results";
}

type Pending =
  | { kind: "start"; key: PhaseKey; opts: StartOptions; llm: LlmStatus }
  | { kind: "review"; key: PhaseKey; file: File; llm: LlmStatus };

export function RunPage({ runId, view, options, llmStatus }: {
  runId: string;
  view?: View;
  options: ServiceOptions | null;
  llmStatus: LlmStatus | null;
}) {
  const { run, error, refresh, setRun } = useRun(runId);
  const active = run?.job != null || !!run?.phases.some((p) => p.status === "running");
  const { forPhase } = useEvents(runId, active);
  const [busy, setBusy] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const [rerun, setRerun] = useState<PhaseKey | null>(null);       // show the start screen again for a finished phase
  const [confirmRerun, setConfirmRerun] = useState<PhaseKey | null>(null);
  const [cancelling, setCancelling] = useState(false);

  // Landing on a run without a phase in the address goes to where the work is.
  useEffect(() => {
    if (run && !view) navigate(`/run/${runId}/${defaultView(run)}`);
  }, [run?.id, view]); // eslint-disable-line react-hooks/exhaustive-deps

  if (error?.code === "run_not_found") {
    return (
      <Alert kind="error" title="This run does not exist">
        It may have been deleted. <a href="#/">Back to all runs</a>
      </Alert>
    );
  }
  if (!run) return error ? <Alert kind="error">{error.message}</Alert> : <p className="muted">Loading…</p>;

  const select = (v: View) => {
    setRerun(null);
    setBanner(null);
    navigate(`/run/${runId}/${v}`);
  };
  const current: View = view ?? defaultView(run);
  const phase = run.phases.find((p) => p.key === current) as Phase | undefined;
  const nextKey = (k: PhaseKey): PhaseKey | null => PHASE_KEYS[PHASE_KEYS.indexOf(k) + 1] ?? null;

  function fail(e: unknown) {
    setBanner(e instanceof ApiError ? e.message : "Something went wrong.");
  }

  async function start(key: PhaseKey, opts: StartOptions = {}) {
    setBusy(true);
    setBanner(null);
    try {
      setRun(await api.start(runId, key, opts));
      setRerun(null);
      setPending(null);
      navigate(`/run/${runId}/${key}`);
    } catch (e) {
      if (e instanceof ApiError && e.code === "llm_unavailable") setPending({ kind: "start", key, opts, llm: e.extra.llm as LlmStatus });
      else {
        setPending(null);
        fail(e);
      }
    } finally {
      setBusy(false);
    }
  }

  async function uploadReview(key: PhaseKey, file: File, llm?: "off") {
    setBusy(true);
    setBanner(null);
    try {
      setRun(await api.uploadReview(runId, key, file, llm));
      setPending(null);
    } catch (e) {
      if (e instanceof ApiError && e.code === "llm_unavailable") setPending({ kind: "review", key, file, llm: e.extra.llm as LlmStatus });
      else {
        setPending(null);
        fail(e);
      }
    } finally {
      setBusy(false);
    }
  }

  async function accept(key: PhaseKey) {
    setBusy(true);
    try {
      const r = await api.accept(runId, key);
      setRun(r);
      navigate(`/run/${runId}/${nextKey(key) ?? "results"}`);
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  }

  async function cancel() {
    setCancelling(true);
    try {
      setRun(await api.cancel(runId));
    } catch (e) {
      fail(e);
    } finally {
      setCancelling(false);
      void refresh();
    }
  }

  function body() {
    if (current === "results") {
      return run!.complete ? <ResultsPage run={run!} /> : <Alert kind="info">The final files are available once Phase D is accepted.</Alert>;
    }
    const p = phase!;
    if (p.status === "locked") {
      const before = PHASE_KEYS[PHASE_KEYS.indexOf(p.key) - 1];
      return (
        <Alert kind="info" title={`Phase ${p.key} is waiting`}>
          Accept the results of Phase {before} first. <a href={`#/run/${runId}/${before}`}>Go to Phase {before}</a>
        </Alert>
      );
    }
    if (p.status === "running") {
      return <RunningPanel phase={p} events={forPhase(p.key)} onCancel={cancel} cancelling={cancelling} title={run!.job?.action === "review" ? `Applying your edits to Phase ${p.key}` : undefined} />;
    }
    if (p.status === "accepted" && p.skipped && rerun !== p.key) {
      return <SkippedPanel phase={p} nextKey={nextKey(p.key)} onGoNext={() => select(nextKey(p.key) ?? "results")} onSetUp={() => setRerun(p.key)} />;
    }
    const showSetup = p.status === "ready" || p.status === "failed" || p.status === "cancelled" || rerun === p.key;
    if (showSetup) {
      const go = (opts: StartOptions = {}) => start(p.key, { ...opts, restart: rerun === p.key || undefined });
      return p.needs_config ? (
        <ConfigPanel run={run!} phase={p} busy={busy} onChange={setRun} onStart={() => go()} onError={fail} />
      ) : (
        <StartPanel run={run!} phase={p} options={options} busy={busy} onStart={go} llmOffline={llmStatus ? !llmStatus.available : false} />
      );
    }
    return (
      <ReviewPanel
        run={run!}
        phase={p}
        nextKey={nextKey(p.key)}
        busy={busy}
        onUpload={(f) => uploadReview(p.key, f)}
        onAccept={() => accept(p.key)}
        onRestart={() => setConfirmRerun(p.key)}
        onGoNext={() => select(nextKey(p.key) ?? "results")}
        onChange={setRun}
        onError={fail}
      />
    );
  }

  const laterWork = (k: PhaseKey) => run.phases.some((p) => p.key > k && p.status !== "locked" && p.status !== "ready");

  return (
    <>
      <div className="crumbs"><a href="#/">All runs</a> / {run.name}</div>
      {banner && <div style={{ marginBottom: 16 }}><Alert kind="error">{banner}</Alert></div>}
      <div className="run-layout">
        <Stepper run={run} view={current} onSelect={select} />
        <div>{body()}</div>
      </div>

      {pending && (
        <LlmDialog
          status={pending.llm}
          phaseTitle={pending.kind === "start" ? `Phase ${pending.key}` : `Applying your edits to Phase ${pending.key}`}
          busy={busy}
          onRetry={() => (pending.kind === "start" ? start(pending.key, pending.opts) : uploadReview(pending.key, pending.file))}
          onContinueWithout={() => (pending.kind === "start" ? start(pending.key, { ...pending.opts, llm: "off" }) : uploadReview(pending.key, pending.file, "off"))}
          onCancel={() => setPending(null)}
        />
      )}
      {confirmRerun && (
        <Modal
          title={`Run Phase ${confirmRerun} again?`}
          onClose={() => setConfirmRerun(null)}
          actions={
            <>
              <button className="btn ghost" onClick={() => setConfirmRerun(null)}>Cancel</button>
              <button className="btn primary" onClick={() => { setRerun(confirmRerun); setConfirmRerun(null); }}>Continue</button>
            </>
          }
        >
          <p>
            The current results of Phase {confirmRerun}
            {laterWork(confirmRerun) ? " and every phase after it" : ""} are discarded when the new run starts. Your original files are kept.
            Edited workbooks you uploaded stay in the run's history but are not re-applied.
          </p>
        </Modal>
      )}
    </>
  );
}
