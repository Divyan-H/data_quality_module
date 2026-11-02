import { useEffect, useState } from "react";
import { api, ApiError, type StartOptions } from "../api";
import { bytes, number, plural } from "../format";
import type { Artifact, CandidateTable, ColumnChoiceTable, ConfigPreview, Phase, PhaseKey, Run, ServiceOptions, SummaryA, SummaryB, SummaryC, SummaryD } from "../types";
import { SummaryAView, SummaryBView, SummaryCView, SummaryDView } from "./summaries";
import { Alert, DownloadLink, FileDrop, Modal, ProblemList, StatusChip } from "./ui";

/* ------------------------------------------------------------------ small pieces */

export function LlmBadge({ phase }: { phase: Phase }) {
  const l = phase.llm;
  if (!phase.uses_llm || !l) return null;
  if (l.requested === "off") return <span className="chip warn" title={l.reason ?? ""}>Rule-based fallback · LLM skipped</span>;
  if (l.used === false) return <span className="chip warn" title={l.reason ?? ""}>LLM not needed · no questions asked</span>;
  return <span className="chip info">LLM · {l.model}{l.calls ? ` · ${plural(l.calls, "call")}` : ""}</span>;
}

function MoreFiles({ runId, artifacts }: { runId: string; artifacts: Artifact[] }) {
  const rest = artifacts.filter((a) => !a.primary);
  if (!rest.length) return null;
  return (
    <details>
      <summary>Other files from this phase ({rest.length})</summary>
      <ul className="files" style={{ marginTop: 8 }}>
        {rest.map((a) => (
          <li key={a.id}>
            <div className="grow">
              <b>{a.label}</b> <span className="muted small">{bytes(a.bytes)}</span>
              <div className="muted small">{a.description}</div>
            </div>
            <a className="btn sm" href={api.fileUrl(runId, a.id)} download>Download</a>
          </li>
        ))}
      </ul>
    </details>
  );
}

function PhaseHeader({ phase }: { phase: Phase }) {
  return (
    <div className="page-head">
      <div>
        <div className="crumbs">Phase {phase.key} of 4</div>
        <h1>{phase.title}</h1>
        <p>{phase.describes}</p>
      </div>
      <div className="row">
        <LlmBadge phase={phase} />
        <StatusChip status={phase.status} />
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ start (A, B) */

export function StartPanel({
  run,
  phase,
  options,
  busy,
  onStart,
  llmOffline,
}: {
  run: Run;
  phase: Phase;
  options: ServiceOptions | null;
  busy: boolean;
  onStart: (o: StartOptions) => void;
  llmOffline: boolean;
}) {
  const [review, setReview] = useState<"near_ties" | "all">("all");
  const [fixes, setFixes] = useState<Record<string, boolean>>({});
  const [choice, setChoice] = useState<ColumnChoiceTable[] | null>(null);
  const [choiceError, setChoiceError] = useState<string | null>(null);
  const [zeroOk, setZeroOk] = useState<Record<string, Set<string>>>({});
  useEffect(() => {
    if (options) {
      setReview(options.llm.review);
      setFixes(Object.fromEntries(options.hygiene_fixes.map((f) => [f.id, f.default])));
    }
  }, [options]);
  useEffect(() => {
    if (phase.key !== "B") return;
    let live = true;
    api.columns(run.id, "B")
      .then((r) => {
        if (!live) return;
        setChoice(r.tables);
        setZeroOk(Object.fromEntries(r.tables.map((t) => [t.ref, new Set(t.columns.filter((c) => c.likely_zero_valid).map((c) => c.name))])));
      })
      .catch((e) => live && setChoiceError(e instanceof ApiError ? e.message : "The columns could not be listed."));
    return () => { live = false; };
  }, [run.id, phase.key]);
  const toggleZero = (ref: string, col: string, on: boolean) =>
    setZeroOk((z) => {
      const next = new Set(z[ref] ?? []);
      if (on) next.add(col);
      else next.delete(col);
      return { ...z, [ref]: next };
    });
  const zeroExpected = (): Record<string, string[]> =>
    Object.fromEntries(Object.entries(zeroOk).filter(([, v]) => v.size).map(([k, v]) => [k, [...v].sort()]));

  const retry = phase.status === "failed" || phase.status === "cancelled";
  return (
    <div className="stack">
      <PhaseHeader phase={phase} />
      {phase.error && (
        <Alert kind="error" title={phase.status === "cancelled" ? "Cancelled" : "This phase failed"}>
          {phase.error.message}
        </Alert>
      )}
      <div className="card">
        <div className="card-body stack">
          {phase.key === "A" && (
            <>
              <dl className="kv">
                <dt>Input</dt>
                <dd>{plural(run.inputs.length, "file")} · {bytes(run.inputs.reduce((a, f) => a + f.bytes, 0))}</dd>
                <dt>Output</dt>
                <dd>One clean table per real data region, a review workbook and the full audit trail.</dd>
                <dt>Local LLM</dt>
                <dd>
                  {llmOffline ? (
                    <span className="chip warn"><span className="dot" />offline – you will be asked how to proceed</span>
                  ) : (
                    <span className="chip ok"><span className="dot" />{options?.llm.model ?? "model"} ready</span>
                  )}{" "}
                  <span className="muted small">reviews header, footer and table-cut decisions the rules are unsure about</span>
                </dd>
              </dl>
              <details>
                <summary>Advanced</summary>
                <div className="field" style={{ marginTop: 10, maxWidth: 420 }}>
                  <label htmlFor="rev">LLM review depth</label>
                  <select id="rev" value={review} onChange={(e) => setReview(e.target.value as "near_ties" | "all")}>
                    <option value="all">Every decision (most thorough, slower)</option>
                    <option value="near_ties">Only where the rules are unsure (faster)</option>
                  </select>
                  <span className="muted small">On a small GPU a full review of a complex workbook can take several minutes.</span>
                </div>
              </details>
            </>
          )}
          {phase.key === "B" && (
            <>
              <dl className="kv">
                <dt>Input</dt>
                <dd>The tables Phase A selected.</dd>
                <dt>Output</dt>
                <dd>Value-clean typed tables, a review workbook (decide each flagged item) and an audit of every change.</dd>
              </dl>
              <details>
                <summary>Automatic fixes ({Object.values(fixes).filter(Boolean).length} of {options?.hygiene_fixes.length ?? 0} on)</summary>
                <p className="muted small" style={{ margin: "8px 0" }}>
                  A fix switched off is still detected: the change is proposed in the review workbook instead of being applied.
                  <b> D-14 (outlier capping) and D-27 (filling an isolated zero) start off</b> because they rewrite genuine spikes and zero weeks
                  in a series, which would hide findings in Phases C and D.
                </p>
                <div className="checks">
                  {options?.hygiene_fixes.map((f) => (
                    <label key={f.id} className="check">
                      <input type="checkbox" checked={fixes[f.id] ?? f.default} onChange={(e) => setFixes({ ...fixes, [f.id]: e.target.checked })} />
                      <span><b>{f.id}</b> {f.what}</span>
                    </label>
                  ))}
                </div>
              </details>
              <details open={!!fixes["D-27"]}>
                <summary>Zero is a valid value in these columns{fixes["D-27"] ? "" : " (used only when D-27 is on)"}</summary>
                <p className="muted small" style={{ margin: "8px 0" }}>
                  With <b>D-27</b> on, a single zero or blank in a daily or weekly series is treated as a missing value and filled with the mean of the day
                  (week) before and after; every fill is listed in the review workbook for you to accept or reject. Tick the columns where zero is a real
                  value (weather, media impressions, spend ...): those are never touched. Longer runs of zeros are left as they are for Phase C.
                </p>
                {choiceError && <Alert kind="warn" title="Columns not listed">{choiceError}</Alert>}
                {!choice && !choiceError && <p className="muted small">Reading the columns…</p>}
                {choice?.map((t) => (
                  <div key={t.ref} style={{ marginBottom: 10 }}>
                    <b>{t.sheet ?? t.file ?? t.ref}</b>{" "}
                    <span className="muted small">
                      {t.dated ? `dated by ${t.period}` : "no date column, so D-27 does not apply to this table"}
                    </span>
                    {t.dated && (
                      <div className="checks">
                        {t.columns.map((c) => (
                          <label key={c.name} className="check">
                            <input type="checkbox" checked={zeroOk[t.ref]?.has(c.name) ?? false} onChange={(e) => toggleZero(t.ref, c.name, e.target.checked)} />
                            <span>
                              <b>{c.name}</b>{" "}
                              <span className="muted small">
                                {c.zeros} of {c.numbers} values are zero{c.name_hint ? " · the name suggests zero can be real" : ""}
                              </span>
                            </span>
                          </label>
                        ))}
                      </div>
                    )}
                  </div>
                ))}
              </details>
            </>
          )}
          <div className="row end">
            <button
              className="btn primary lg"
              disabled={busy}
              onClick={() => onStart(phase.key === "A" ? { llm_review: review } : { fixes, ...(Object.keys(zeroExpected()).length ? { zero_expected: zeroExpected() } : {}) })}
            >
              {retry ? "Try again" : `Run Phase ${phase.key}`}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ config (C, D) */

function Preview({ phase, p }: { phase: PhaseKey; p: ConfigPreview }) {
  return (
    <div className="stack" style={{ gap: 10 }}>
      <dl className="kv">
        <dt>Tables</dt>
        <dd>
          {(p.tables ?? []).map((t) => (
            <div key={t.ref}>
              <span className={`chip ${t.checked ? "ok" : "warn"}`}>{t.checked ? "Checked" : "Not checked"}</span>{" "}
              {t.sheet ?? t.file ?? t.ref} · {number(t.rows)} rows{t.reason ? <span className="muted small"> · {t.reason}</span> : null}
            </div>
          ))}
        </dd>
        <dt>Period column</dt>
        <dd>{p.period}</dd>
        {phase === "C" && (
          <>
            {p.series && p.series.length > 0 && (<><dt>Series</dt><dd>one per {p.series.join(" × ")}</dd></>)}
            <dt>Variables</dt>
            <dd>{p.checked} of {p.variables} will be checked{p.ignored && p.ignored.length > 0 && <span className="muted"> · not checked (no Bucket): {p.ignored.slice(0, 8).join(", ")}{p.ignored.length > 8 ? "…" : ""}</span>}</dd>
            <dt>No-execution run</dt>
            <dd>{p.min_zero_weeks} consecutive zero periods</dd>
          </>
        )}
        {phase === "D" && (<><dt>Spend/Support pairs</dt><dd>{p.pairs}</dd></>)}
      </dl>
      {phase === "D" && p.rules && (
        <div className="table-wrap">
          <table className="t">
            <thead><tr><th>Rule</th><th>Threshold</th><th>Will run</th></tr></thead>
            <tbody>
              {p.rules.map((r) => (
                <tr key={r.key}>
                  <td><code>{r.code}</code> {r.label}</td>
                  <td>{r.threshold ?? "–"}</td>
                  <td><span className={`chip ${r.active ? "ok" : "warn"}`}>{r.active ? "Yes" : "No threshold"}</span></td>
                </tr>
              ))}
              {p.ratios?.map((r) => (
                <tr key={r.name}>
                  <td><code>N-18</code> {r.name} <span className="muted small">({r.numerator} / {r.denominator})</span></td>
                  <td>{r.threshold ?? "–"}</td>
                  <td><span className={`chip ${r.active ? "ok" : "warn"}`}>{r.active ? "Yes" : "No threshold"}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      {p.warnings.map((w, i) => <Alert key={i} kind="warn">{w}</Alert>)}
    </div>
  );
}

export function ConfigPanel({
  run,
  phase,
  busy,
  onChange,
  onStart,
  onError,
}: {
  run: Run;
  phase: Phase;
  busy: boolean;
  onChange: (r: Run) => void;
  onStart: () => void;
  onError: (e: unknown) => void;
}) {
  const key = phase.key;
  const cfg = phase.config;
  const [tables, setTables] = useState<CandidateTable[] | null>(null);
  const [working, setWorking] = useState<string | null>(null);
  const [forceSetup, setForceSetup] = useState(false);
  const [problem, setProblem] = useState<{ message: string; problems: { message: string }[] } | null>(null);

  useEffect(() => {
    let live = true;
    api.tables(run.id, key).then((r) => {
      if (!live) return;
      setTables(r.tables);
    }).catch(onError);
    return () => { live = false; };
  }, [run.id, key]); // eslint-disable-line react-hooks/exhaustive-deps

  async function download() {
    setWorking("Preparing the pre-filled workbook…");
    try {
      const res = await fetch(api.templateUrl(run.id, key));
      if (!res.ok) throw new ApiError(res.status, "template", ((await res.json()).error?.message as string) ?? "Could not build the template.");
      const blob = await res.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = `phase_${key}_client_config_prefilled.xlsx`;
      a.click();
      URL.revokeObjectURL(a.href);
    } catch (e) {
      onError(e);
    } finally {
      setWorking(null);
    }
  }

  async function upload(files: { file: File }[]) {
    setWorking("Checking your workbook against the data…");
    setProblem(null);
    try {
      onChange(await api.uploadConfig(run.id, key, files[0].file, ""));
    } catch (e) {
      if (e instanceof ApiError && e.code !== "unreachable") {
        setProblem({ message: e.message, problems: (e.extra.problems as { message: string }[]) ?? [] });
      } else onError(e);
    } finally {
      setWorking(null);
    }
  }

  async function skip(reason: string) {
    setWorking("Skipping…");
    try {
      onChange(await api.skip(run.id, key, reason));
    } catch (e) {
      onError(e);
    } finally {
      setWorking(null);
    }
  }

  const retry = phase.status === "failed" || phase.status === "cancelled";
  const notApplicable = tables !== null && tables.length > 0 && !tables.some((t) => t.weekly);
  if (notApplicable && !forceSetup) {
    const why = tables!.map((t) => `${t.file ?? t.ref}${t.sheet ? ` · ${t.sheet}` : ""}: ${t.note}`).join(" ");
    return (
      <div className="stack">
        <PhaseHeader phase={phase} />
        <Alert kind="warn" title={`Phase ${key} does not apply to this data`}>
          <ul>
            {tables!.map((t) => (
              <li key={t.ref}>
                <b style={{ display: "inline" }}>{t.file ?? t.ref}{t.sheet ? ` · ${t.sheet}` : ""}</b>: {t.note}
              </li>
            ))}
          </ul>
          <p style={{ marginTop: 8 }}>
            {key === "C"
              ? "Phase C checks dated time series (runs of zero periods, repeated values, missing or duplicate periods) and needs a date column and at least one numeric measure. "
              : "Phase D compares periods of a measure (month, quarter, year) and needs a date column and at least one numeric measure. "}
            You can skip it and continue with the cleaned data from the earlier phases. Nothing is lost: the skip is recorded in the run.
          </p>
        </Alert>
        <div className="row end">
          <button className="btn ghost" onClick={() => setForceSetup(true)} disabled={!!working || busy}>Set it up anyway</button>
          <button className="btn primary lg" onClick={() => skip(why)} disabled={!!working || busy}>
            {working ?? `Skip Phase ${key} and continue`}
          </button>
        </div>
      </div>
    );
  }
  return (
    <div className="stack">
      <PhaseHeader phase={phase} />
      {phase.error && <Alert kind="error" title={phase.status === "cancelled" ? "Cancelled" : "This phase failed"}>{phase.error.message}</Alert>}
      {cfg.uploaded && !cfg.valid && !problem && cfg.error && (
        <Alert kind="warn" title="Your config needs another look">{cfg.error.message}</Alert>
      )}
      {key === "C" && tables?.some((t) => t.from_monthly) && (
        <Alert kind="info" title="Monthly data: it is turned into weeks before the checks">
          Each monthly figure is split over the days of its Monday-to-Sunday weeks. On the <b>Column Setup</b> sheet of the config, set each
          column to <b>Sum</b> (orders, spend, clicks) or <b>Average</b> (a rate such as AOV or margin %). The repeated-value checks (N-08, N-09) are not
          run on these weeks, because the figures repeat by construction.
        </Alert>
      )}
      <div className="card">
        <div className="card-head"><h2><span className="num-step">1</span>Download the pre-filled config</h2></div>
        <div className="card-body stack">
          <p className="muted">
            Green cells are filled from your data (columns, variable types, Spend/Support pairs, buckets, period labels). Yellow cells are for
            you{key === "D" ? ": the thresholds, ratio definitions and N-20 grouping. A rule with no threshold does not run" : ": the Data Check Specifications and anything the names could not decide"}.
          </p>
          {tables && tables.length > 1 && (
            <p className="muted small">One config covers all {tables.length} tables ({tables.map((t) => t.sheet ?? t.file ?? t.ref).join(", ")}); each table gets its own report.</p>
          )}
          <div className="row">
            <button className="btn primary" onClick={download} disabled={!!working || busy || tables === null}>Download pre-filled config (.xlsx)</button>
            {working?.startsWith("Preparing") && <span className="muted small">{working}</span>}
          </div>
        </div>
      </div>
      <div className="card">
        <div className="card-head"><h2><span className="num-step">2</span>Fill it in and upload it</h2></div>
        <div className="card-body stack">
          <FileDrop accept={[".xlsx"]} compact title={cfg.uploaded ? "Upload a different filled config" : "Drop the filled config workbook here"} disabled={!!working || busy} onFiles={upload} />
          {working?.startsWith("Checking") && <p className="muted">{working}</p>}
          {problem && (
            <Alert kind="error" title="This config was not accepted">
              <p>{problem.message}</p>
              {problem.problems.length > 0 && <ProblemList problems={problem.problems} />}
            </Alert>
          )}
          {cfg.valid && cfg.preview && (
            <div className="stack" style={{ gap: 10 }}>
              <Alert kind="ok" title={`Config accepted${cfg.name ? ` · ${cfg.name}` : ""}`}>It fits the data. This is what Phase {key} will do:</Alert>
              <Preview phase={key} p={cfg.preview} />
            </div>
          )}
        </div>
      </div>
      <div className="row end">
        <button className="btn ghost" disabled={!!working || busy} onClick={() => skip("Skipped by the analyst.")}>Skip Phase {key}</button>
        {cfg.uploaded && (
          <button className="btn ghost" disabled={!!working || busy} onClick={() => api.recheckConfig(run.id, key).then(onChange).catch((e) => onError(e))}>
            Re-check config against the data
          </button>
        )}
        <button className="btn primary lg" disabled={!cfg.valid || busy || !!working} onClick={onStart}>
          {retry ? "Try again" : `Run Phase ${key}`}
        </button>
      </div>
    </div>
  );
}

/* ------------------------------------------------------------------ review (finished phase) */

export function ReviewPanel({
  run,
  phase,
  nextKey,
  busy,
  onUpload,
  onAccept,
  onRestart,
  onGoNext,
  onChange,
  onError,
  onOpenAgent,
}: {
  run: Run;
  phase: Phase;
  nextKey: PhaseKey | null;
  busy: boolean;
  onUpload: (file: File) => void;
  onAccept: () => void;
  onRestart: () => void;
  onGoNext: () => void;
  onChange: (r: Run) => void;
  onError: (e: unknown) => void;
  /** Present when this phase supports the Fix-with-agent screen. */
  onOpenAgent?: () => void;
}) {
  const [summary, setSummary] = useState<unknown>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [choices, setChoices] = useState(0);
  const version = `${phase.attempt}:${phase.uploads.length}:${phase.finished_at}:${choices}:${phase.agent?.last_at ?? ""}`;
  const selectTables = async (refs: string[]) => {
    try {
      onChange(await api.selectTables(run.id, refs));
      setChoices((n) => n + 1);
    } catch (e) {
      onError(e);
    }
  };

  useEffect(() => {
    let live = true;
    setLoadError(null);
    api.summary(run.id, phase.key).then((s) => live && setSummary(s)).catch((e) => live && setLoadError(e.message));
    return () => { live = false; };
  }, [run.id, phase.key, version]);

  const primaries = phase.artifacts.filter((a) => a.primary);
  const accepted = phase.status === "accepted";
  const last = phase.uploads.filter((u) => u.ok).length;
  const [confirm, setConfirm] = useState<File | null>(null);
  const downstream = run.phases.some((p) => p.key > phase.key && p.status !== "locked" && p.status !== "ready");

  return (
    <div className="stack">
      <PhaseHeader phase={phase} />
      {phase.upload_error && (
        <Alert kind="error" title="Your uploaded workbook was not accepted. Nothing was changed.">
          <p>{phase.upload_error.message}</p>
          {phase.upload_error.problems && phase.upload_error.problems.length > 0 && <ProblemList problems={phase.upload_error.problems} />}
        </Alert>
      )}
      {last > 0 && !phase.upload_error && (
        <Alert kind="ok">Your last edited workbook was applied and the phase was re-checked ({plural(last, "upload")} so far).</Alert>
      )}
      <div className="card">
        <div className="card-head"><h2>Results</h2></div>
        <div className="card-body">
          {loadError && <Alert kind="error">{loadError}</Alert>}
          {!summary && !loadError && <p className="muted">Loading the results…</p>}
          {!!summary && phase.key === "A" && <SummaryAView s={summary as SummaryA} onSelect={busy ? undefined : selectTables} />}
          {!!summary && phase.key === "B" && <SummaryBView s={summary as SummaryB} />}
          {!!summary && phase.key === "C" && <SummaryCView s={summary as SummaryC} />}
          {!!summary && phase.key === "D" && <SummaryDView s={summary as SummaryD} />}
        </div>
      </div>
      {onOpenAgent && (
        <div className="card card-pad agent-entry">
          <div className="grow">
            <h3>Fix with the agent</h3>
            <p className="muted small">
              See the data and the open issues side by side and say what to change: “accept all date fixes except row 14”,
              “Head Office is Bangalore”, “Spend is in thousands”. Every change is previewed first and logged in the audit.
              {phase.agent?.applied ? ` ${plural(phase.agent.applied, "change")} applied so far.` : ""}
            </p>
          </div>
          <button className="btn primary" onClick={onOpenAgent} disabled={busy}>Fix with agent</button>
        </div>
      )}
      <div className="card">
        <div className="card-head">
          <h2>Your review</h2>
          <span className="muted small">Edits are optional</span>
        </div>
        <div className="card-body stack">
          <div className="row" style={{ alignItems: "flex-start" }}>
            <div className="grow">
              <h3><span className="num-step">1</span>Download {primaries.length > 1 ? `the ${primaries.length} reports (one per table)` : phase.review_name}</h3>
              {primaries.length === 1 && <p className="muted small" style={{ marginLeft: 30 }}>{primaries[0].description}</p>}
            </div>
            {primaries.length === 1 && <DownloadLink primary href={api.fileUrl(run.id, primaries[0].id)}>Download workbook</DownloadLink>}
          </div>
          {primaries.length > 1 && (
            <ul className="files" style={{ marginLeft: 30 }}>
              {primaries.map((a) => (
                <li key={a.id}>
                  <div className="grow"><b>{a.label}</b> <span className="muted small">{bytes(a.bytes)}</span><div className="muted small">{a.description}</div></div>
                  <DownloadLink primary href={api.fileUrl(run.id, a.id)}>Download</DownloadLink>
                </li>
              ))}
            </ul>
          )}
          <div>
            <h3><span className="num-step">2</span>Edit it in Excel and upload it back</h3>
            <p className="muted small" style={{ margin: "0 0 8px 30px" }}>
              Only change the columns the workbook marks as editable. The file is checked before anything is applied; if it does not pass, nothing changes.
              {downstream ? " Applying edits discards the later phases, which then need to be run again." : ""}
            </p>
            <FileDrop accept={[".xlsx"]} compact title="Drop the edited workbook here" disabled={busy} onFiles={(f) => (downstream ? setConfirm(f[0].file) : onUpload(f[0].file))} />
          </div>
          <MoreFiles runId={run.id} artifacts={phase.artifacts} />
        </div>
      </div>
      <div className="card card-pad row" style={{ justifyContent: "space-between" }}>
        <div>
          {accepted ? <b>Phase {phase.key} is accepted.</b> : <b>No edits to make?</b>}
          <div className="muted small">
            {phase.blocked_reason ?? (accepted ? "Its results feed the next phase." : nextKey ? `Accepting unlocks Phase ${nextKey}.` : "Accepting finishes the pipeline and opens the final files.")}
          </div>
        </div>
        <div className="row">
          <button className="btn" onClick={onRestart} disabled={busy}>Run this phase again</button>
          {accepted ? (
            <button className="btn primary lg" onClick={onGoNext}>{nextKey ? `Go to Phase ${nextKey}` : "Open final files"}</button>
          ) : (
            <button className="btn primary lg" onClick={onAccept} disabled={busy || !phase.can_continue}>
              {nextKey ? `Accept and continue to Phase ${nextKey}` : "Accept and finish"}
            </button>
          )}
        </div>
      </div>
      {confirm && (
        <Modal
          title="Apply your edits?"
          onClose={() => setConfirm(null)}
          actions={
            <>
              <button className="btn ghost" onClick={() => setConfirm(null)}>Cancel</button>
              <button className="btn primary" onClick={() => { onUpload(confirm); setConfirm(null); }}>Apply and discard later phases</button>
            </>
          }
        >
          <p>Phases after {phase.key} already ran on the current results. Applying your edits changes the data they used, so they will be discarded and need to be run again.</p>
        </Modal>
      )}
    </div>
  );
}

/* ------------------------------------------------------------------ skipped (C, D) */

export function SkippedPanel({ phase, nextKey, onGoNext, onSetUp }: { phase: Phase; nextKey: PhaseKey | null; onGoNext: () => void; onSetUp: () => void }) {
  return (
    <div className="stack">
      <PhaseHeader phase={phase} />
      <Alert kind="info" title={`Phase ${phase.key} was skipped`}>
        <p>{phase.skip_reason ?? "Skipped by the analyst."}</p>
        <p style={{ marginTop: 6 }}>The next phase works from the cleaned data of the phases before it. The run records that this phase did not run.</p>
      </Alert>
      <div className="row end">
        <button className="btn" onClick={onSetUp}>Set it up after all</button>
        <button className="btn primary lg" onClick={onGoNext}>{nextKey ? `Go to Phase ${nextKey}` : "Open final files"}</button>
      </div>
    </div>
  );
}
