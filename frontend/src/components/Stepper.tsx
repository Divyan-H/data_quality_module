import type { Phase, PhaseKey, Run } from "../types";

export type View = PhaseKey | "results";

function mark(p: Phase): { cls: string; text: string } {
  switch (p.status) {
    case "accepted":
      return { cls: "done", text: "✓" };
    case "failed":
    case "cancelled":
      return { cls: "failed", text: "!" };
    case "running":
      return { cls: "running", text: "…" };
    case "locked":
      return { cls: "", text: p.key };
    default:
      return { cls: "active", text: p.key };
  }
}

const SUB: Record<string, string> = {
  locked: "Waiting for the previous phase",
  ready: "Ready to start",
  running: "Running…",
  review: "Check the results",
  accepted: "Accepted",
  failed: "Failed – open to retry",
  cancelled: "Cancelled",
};

export function Stepper({ run, view, onSelect }: { run: Run; view: View; onSelect: (v: View) => void }) {
  return (
    <nav className="card stepper" aria-label="Pipeline steps">
      {run.phases.map((p, i) => {
        const m = mark(p);
        const locked = p.status === "locked";
        return (
          <div key={p.key}>
            {i > 0 && <div className="step-line" />}
            <button
              className="step"
              aria-current={view === p.key ? "step" : undefined}
              aria-disabled={locked}
              onClick={() => !locked && onSelect(p.key)}
            >
              <span className={`step-mark ${m.cls}`}>{m.text}</span>
              <span>
                <div className="step-title">
                  Phase {p.key} · {p.title}
                </div>
                <div className="step-sub">{p.skipped ? "Skipped" : p.needs_config && p.status === "ready" && !p.config.valid ? "Needs your config file" : SUB[p.status]}</div>
              </span>
            </button>
          </div>
        );
      })}
      <div className="step-line" />
      <button className="step" aria-current={view === "results" ? "step" : undefined} aria-disabled={!run.complete} onClick={() => run.complete && onSelect("results")}>
        <span className={`step-mark ${run.complete ? "done" : ""}`}>{run.complete ? "✓" : "★"}</span>
        <span>
          <div className="step-title">Results</div>
          <div className="step-sub">{run.complete ? "All final files" : "Available when Phase D is accepted"}</div>
        </span>
      </button>
    </nav>
  );
}
