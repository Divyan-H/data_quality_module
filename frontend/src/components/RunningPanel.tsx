import { useEffect, useRef } from "react";
import { duration } from "../format";
import { useNow } from "../hooks";
import type { EventItem, Phase } from "../types";

const EXPECTED: Record<string, string[]> = {
  A: ["scanning", "converting", "reading facts", "finding regions", "LLM review", "writing tables", "finalizing", "writing review workbook"],
  B: ["cleaning tables", "summarising"],
  C: ["checking the weekly table", "summarising"],
  D: ["checking trends", "summarising"],
};
const HIDE_UNLESS_SEEN = new Set(["converting"]);

const LABEL: Record<string, string> = {
  scanning: "Scanning files",
  converting: "Converting legacy format",
  "reading facts": "Reading workbook structure",
  "finding regions": "Finding tables and headers",
  "LLM review": "Local LLM review of uncertain decisions",
  "writing tables": "Writing clean tables",
  finalizing: "Collecting findings",
  "writing review workbook": "Building the review workbook",
  "cleaning tables": "Cleaning values table by table",
  "checking the weekly table": "Running time-series checks",
  "checking trends": "Running trend checks",
  "checking the uploaded workbook": "Checking your uploaded workbook",
  "applying your review": "Applying your decisions",
  "applying your review and re-checking": "Applying your edits and re-checking",
  summarising: "Preparing the results",
  starting: "Starting",
};

export interface Derived {
  stages: { name: string; state: "done" | "now" | "todo" }[];
  current: string | null;
  detail: string | null;
  pct: number | null;
  progress: { done: number; total: number; label: string } | null;
  logs: Extract<EventItem, { type: "log" }>[];
}

/** Fold a job's events into what the panel shows. Pure, so it is easy to test. */
export function derive(events: EventItem[], phase: string, usesLlm: boolean): Derived {
  const seen: string[] = [];
  let detail: string | null = null;
  let progress: Derived["progress"] = null;
  const logs: Derived["logs"] = [];
  let pct: number | null = null;
  let lastStage: string | null = null;
  for (const e of events) {
    if (e.type === "stage") {
      if (e.stage !== "starting" && !seen.includes(e.stage)) seen.push(e.stage);
      if (e.stage !== lastStage) {
        detail = null;
        logs.push({ seq: e.seq, t: e.t, job: e.job, phase: e.phase, type: "log", level: "info", message: `▸ ${LABEL[e.stage] ?? e.stage}` });
      }
      lastStage = e.stage;
      if (e.detail) detail = e.detail;
      pct = e.pct;
    } else if (e.type === "progress") progress = { done: e.done, total: e.total, label: e.label };
    else logs.push(e);
  }
  const current = seen.length ? seen[seen.length - 1] : events.length ? "starting" : null;
  let expected = (EXPECTED[phase] ?? []).filter((s) => (usesLlm || s !== "LLM review") && (!HIDE_UNLESS_SEEN.has(s) || seen.includes(s)));
  const extra = seen.filter((s) => !expected.includes(s));
  expected = [...extra, ...expected];
  const idx = current ? expected.indexOf(current) : -1;
  const stages = expected.map((name, i) => ({
    name,
    state: (name === current ? "now" : seen.includes(name) || (idx >= 0 && i < idx) ? "done" : "todo") as "done" | "now" | "todo",
  }));
  return { stages, current, detail, pct, progress, logs };
}

export function RunningPanel({
  phase,
  events,
  onCancel,
  cancelling,
  title,
}: {
  phase: Phase;
  events: EventItem[];
  onCancel: () => void;
  cancelling: boolean;
  title?: string;
}) {
  const now = useNow();
  const d = derive(events, phase.key, phase.llm?.requested === "on");
  const started = phase.started_at ? new Date(phase.started_at).getTime() : now;
  const logRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = logRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [d.logs.length]);

  const stageCount = d.stages.length || 1;
  const doneCount = d.stages.filter((s) => s.state === "done").length;
  const barPct = d.progress ? (d.progress.done / d.progress.total) * 100 : d.stages.length ? (doneCount / stageCount) * 100 : null;

  return (
    <div className="card">
      <div className="card-head">
        <div>
          <h2>{title ?? `Phase ${phase.key} is running`}</h2>
          <p className="muted small">Elapsed {duration((now - started) / 1000)}. You can leave this page; the run keeps going.</p>
        </div>
        <button className="btn danger" onClick={onCancel} disabled={cancelling}>
          {cancelling ? "Cancelling…" : "Cancel"}
        </button>
      </div>
      <div className="card-body stack">
        <div>
          <div className={`bar ${barPct === null ? "indeterminate" : ""}`} role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={barPct === null ? undefined : Math.round(barPct)}>
            <i style={barPct === null ? undefined : { width: `${Math.max(4, barPct)}%` }} />
          </div>
          <p className="small muted" style={{ marginTop: 6 }}>
            {d.progress ? `${d.progress.done} of ${d.progress.total} ${d.progress.label}` : d.detail ?? (d.current ? LABEL[d.current] ?? d.current : "Starting the worker…")}
          </p>
        </div>
        {d.stages.length > 0 && (
          <ol className="timeline" aria-label="Steps">
            {d.stages.map((s) => (
              <li key={s.name} className={s.state}>
                <span className="tl-mark">{s.state === "done" ? "✓" : ""}</span>
                <span>{LABEL[s.name] ?? s.name}</span>
                {s.state === "now" && d.detail && <span className="muted small">· {d.detail}</span>}
              </li>
            ))}
          </ol>
        )}
        <div>
          <h3 style={{ marginBottom: 8 }}>Live log</h3>
          <div className="log" ref={logRef} role="log" aria-live="polite">
            {d.logs.length === 0 && <span className="t">Waiting for the first message…</span>}
            {d.logs.map((l) => (
              <div key={l.seq} className={l.level === "warn" ? "w" : l.level === "error" ? "e" : ""}>
                <span className="t">{new Date(l.t).toLocaleTimeString()}</span>
                {l.message}
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
