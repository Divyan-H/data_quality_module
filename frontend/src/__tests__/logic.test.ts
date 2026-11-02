import { describe, expect, it } from "vitest";
import { derive } from "../components/RunningPanel";
import { defaultView } from "../pages/RunPage";
import { checkFiles, type Staged } from "../pages/Home";
import { bytes, duration, extension, number, plural, timeAgo } from "../format";
import { parseRoute } from "../hooks";
import type { EventItem, Run } from "../types";

const file = (name: string, size = 100): Staged => ({ file: new File([new Uint8Array(size)], name), path: name });

describe("format", () => {
  it("formats sizes", () => {
    expect(bytes(512)).toBe("512 B");
    expect(bytes(2048)).toBe("2.0 KB");
    expect(bytes(5 * 1024 ** 2)).toBe("5.0 MB");
    expect(bytes(3 * 1024 ** 3)).toBe("3.0 GB");
  });
  it("formats durations", () => {
    expect(duration(7)).toBe("7s");
    expect(duration(125)).toBe("2m 05s");
    expect(duration(3720)).toBe("1h 02m");
    expect(duration(-5)).toBe("0s");
  });
  it("formats numbers, plurals, extensions", () => {
    expect(number(12345)).toBe("12,345");
    expect(number(null)).toBe("–");
    expect(plural(1, "file")).toBe("1 file");
    expect(plural(2, "file")).toBe("2 files");
    expect(extension("A.B.XLSX")).toBe(".xlsx");
    expect(extension("noext")).toBe("");
  });
  it("says how long ago", () => {
    const now = Date.parse("2026-10-11T12:00:00Z");
    expect(timeAgo("2026-10-11T11:59:50Z", now)).toBe("just now");
    expect(timeAgo("2026-10-11T11:30:00Z", now)).toBe("30 min ago");
    expect(timeAgo("2026-10-11T09:00:00Z", now)).toBe("3 h ago");
  });
});

describe("routes", () => {
  it("parses the hash", () => {
    expect(parseRoute("")).toEqual({ name: "home" });
    expect(parseRoute("#/")).toEqual({ name: "home" });
    expect(parseRoute("#/run/abc123")).toEqual({ name: "run", runId: "abc123", view: undefined });
    expect(parseRoute("#/run/abc123/C")).toEqual({ name: "run", runId: "abc123", view: "C" });
    expect(parseRoute("#/run/abc123/results")).toEqual({ name: "run", runId: "abc123", view: "results" });
  });
  it("ignores an unknown phase", () => {
    expect(parseRoute("#/run/abc/Z").view).toBeUndefined();
    expect(parseRoute("#/run/abc/AB").view).toBeUndefined();
  });
});

describe("file checks on the home screen", () => {
  const allowed = [".xlsx", ".csv"];
  it("accepts supported files and refuses the rest with a reason", () => {
    const r = checkFiles([file("a.xlsx"), file("b.exe"), file("c.csv", 0), file("noext")], allowed, [], 10);
    expect(r.ok.map((s) => s.path)).toEqual(["a.xlsx"]);
    expect(r.rejected.map((x) => x.name)).toEqual(["b.exe", "c.csv", "noext"]);
    expect(r.rejected[1].reason).toMatch(/empty/);
  });
  it("skips office lock files and OS clutter silently", () => {
    const r = checkFiles([file("~$a.xlsx"), file("Thumbs.db"), file(".DS_Store")], allowed, [], 10);
    expect(r.ok).toHaveLength(0);
    expect(r.rejected).toHaveLength(0);
  });
  it("does not add the same path twice, nor go past the limit", () => {
    const have = [file("a.xlsx", 600_000)];
    const r = checkFiles([file("a.xlsx"), file("big.csv", 600_000)], allowed, have, 1);
    expect(r.ok).toHaveLength(0);
    expect(r.rejected.map((x) => x.reason)).toEqual(["already added", "would pass the 1 MB limit"]);
  });
});

describe("progress derived from events", () => {
  const stage = (stage: string, detail: string | null = null, seq = 1): EventItem => ({ seq, t: "t", job: "j", phase: "A", type: "stage", stage, pct: 0, detail });
  it("marks earlier stages done and the last one current", () => {
    const d = derive([stage("starting"), stage("scanning"), stage("reading facts"), stage("finding regions")], "A", false);
    expect(d.current).toBe("finding regions");
    expect(d.stages.find((s) => s.name === "scanning")?.state).toBe("done");
    expect(d.stages.find((s) => s.name === "finding regions")?.state).toBe("now");
    expect(d.stages.find((s) => s.name === "finalizing")?.state).toBe("todo");
  });
  it("shows the LLM step only when the LLM is used", () => {
    expect(derive([stage("scanning")], "A", true).stages.map((s) => s.name)).toContain("LLM review");
    expect(derive([stage("scanning")], "A", false).stages.map((s) => s.name)).not.toContain("LLM review");
  });
  it("keeps the detail of the current stage and drops it when the stage changes", () => {
    const e = [stage("LLM review", "3 call(s)"), stage("LLM review", "4 call(s)", 2)];
    expect(derive(e, "A", true).detail).toBe("4 call(s)");
    expect(derive([...e, stage("writing tables", null, 3)], "A", true).detail).toBeNull();
  });
  it("collects logs and the table progress", () => {
    const e: EventItem[] = [
      { seq: 1, t: "t", job: "j", phase: "B", type: "log", level: "warn", message: "hello" },
      { seq: 2, t: "t", job: "j", phase: "B", type: "progress", done: 2, total: 5, label: "tables cleaned" },
    ];
    const d = derive(e, "B", false);
    expect(d.logs).toHaveLength(1);
    expect(d.progress).toEqual({ done: 2, total: 5, label: "tables cleaned" });
  });
  it("copes with no events yet", () => {
    const d = derive([], "C", false);
    expect(d.current).toBeNull();
    expect(d.stages.every((s) => s.state === "todo")).toBe(true);
  });
  it("adds a stage it was not told about", () => {
    const d = derive([stage("applying your review")], "B", false);
    expect(d.stages[0].name).toBe("applying your review");
  });
});

describe("where a run opens", () => {
  const run = (statuses: string[], complete = false) =>
    ({ complete, phases: ["A", "B", "C", "D"].map((key, i) => ({ key, status: statuses[i] })) }) as unknown as Run;
  it("goes to the first phase that is not accepted", () => {
    expect(defaultView(run(["accepted", "review", "locked", "locked"]))).toBe("B");
    expect(defaultView(run(["accepted", "accepted", "ready", "locked"]))).toBe("C");
    expect(defaultView(run(["ready", "locked", "locked", "locked"]))).toBe("A");
  });
  it("opens the results when complete", () => {
    expect(defaultView(run(["accepted", "accepted", "accepted", "accepted"], true))).toBe("results");
  });
});

describe("legacy formats", () => {
  it("refuses .xls/.xlsb/.ods when the converter is not installed, with the way out", () => {
    const r = checkFiles([file("old.xls"), file("new.xlsx")], [".xls", ".xlsx"], [], 10, [".xls", ".xlsb", ".ods"]);
    expect(r.ok.map((s) => s.path)).toEqual(["new.xlsx"]);
    expect(r.rejected[0].reason).toMatch(/LibreOffice.*save the file as \.xlsx/);
  });
  it("accepts them when it is installed", () => {
    expect(checkFiles([file("old.xls")], [".xls"], [], 10, []).ok).toHaveLength(1);
  });
});
