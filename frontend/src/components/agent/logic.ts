import type { ChangeSetView, IssueGroup, Phase } from "../../types";

/** Changes whenever the phase's data may have changed: refetch issues, rows and the session then. */
export function dataVersion(phase: Phase): string {
  return [phase.status, phase.attempt, phase.finished_at, phase.uploads.length, phase.agent?.last_at ?? "", phase.agent?.applied ?? 0, phase.agent?.undone ?? 0].join("|");
}

/** One-click instructions for an issue group: what an analyst most often says about it. */
export function suggestionsFor(g: IssueGroup): { label: string; text: string }[] {
  const where = g.column ? ` in ${g.column}` : "";
  const out = [
    { label: "Accept", text: `accept the ${g.rule} items${where}` },
    { label: "Keep as is", text: `keep the ${g.rule} items${where}` },
  ];
  if (g.rule === "D-29") out.push({ label: "Split rows", text: `split the multi-value cells${where}` });
  else out.push({ label: "Restore original", text: `reject the ${g.rule} items${where}` });
  return out;
}

export const STARTERS = ["summary", "help"];

export function statusLabel(cs: ChangeSetView): { text: string; tone: "" | "ok" | "warn" | "bad" | "info" } {
  switch (cs.status) {
    case "proposed": return { text: "Waiting for you", tone: "info" };
    case "applied": return { text: "Applied", tone: "ok" };
    case "undone": return { text: "Undone", tone: "" };
    case "discarded": return { text: "Discarded", tone: "" };
    case "stale": return { text: "Out of date", tone: "warn" };
    case "failed": return { text: "Failed", tone: "bad" };
  }
}

export function show(v: string | null | undefined): string {
  return v === null || v === undefined ? "∅" : v === "" ? "''" : v;
}

export function tableLabel(t: { ref: string; file: string | null; sheet: string | null }): string {
  return [t.file, t.sheet].filter(Boolean).join(" · ") || t.ref;
}
