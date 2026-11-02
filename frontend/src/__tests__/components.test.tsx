import { fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { DataTable } from "../components/DataTable";
import { LlmChip } from "../components/LlmChip";
import { LlmDialog } from "../components/LlmDialog";
import { LlmBadge } from "../components/panels";
import { Stepper } from "../components/Stepper";
import { FileDrop, ProblemList, StatusChip } from "../components/ui";
import type { LlmStatus, Phase, PhaseKey, Run, Status } from "../types";

const down: LlmStatus = {
  available: false, url: "http://localhost:11434", model: "llama3.2:3b", reason: "llm_unreachable",
  message: "Ollama is not reachable. Start it with `ollama serve` in a terminal.", version: null, digest: null, installed: null,
};

describe("LlmDialog", () => {
  const setup = (status = down) => {
    const fns = { onRetry: vi.fn(), onContinueWithout: vi.fn(), onCancel: vi.fn() };
    render(<LlmDialog status={status} phaseTitle="Phase A" busy={false} {...fns} />);
    return fns;
  };
  it("explains the problem and offers the three choices", async () => {
    const f = setup();
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText(/not reachable/)).toBeInTheDocument();
    expect(within(dialog).getByText(/ollama serve/, { selector: "code" })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Continue without the LLM" }));
    await userEvent.click(screen.getByRole("button", { name: "Check again" }));
    await userEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(f.onContinueWithout).toHaveBeenCalledOnce();
    expect(f.onRetry).toHaveBeenCalledOnce();
    expect(f.onCancel).toHaveBeenCalledOnce();
  });
  it("closes on Escape", async () => {
    const f = setup();
    await userEvent.keyboard("{Escape}");
    expect(f.onCancel).toHaveBeenCalled();
  });
  it("tells the analyst which model to install when it is missing", () => {
    setup({ ...down, reason: "llm_model_missing", message: "Ollama is running but the model is not installed.", installed: ["qwen2.5:7b"] });
    expect(screen.getByText("ollama pull llama3.2:3b", { selector: "code" })).toBeInTheDocument();
    expect(screen.getByText(/Installed now: qwen2.5:7b/)).toBeInTheDocument();
  });
  it("disables the choices while a request is in flight", () => {
    render(<LlmDialog status={down} phaseTitle="Phase B" busy onRetry={vi.fn()} onContinueWithout={vi.fn()} onCancel={vi.fn()} />);
    for (const name of ["Cancel", "Check again", "Continue without the LLM"]) expect(screen.getByRole("button", { name })).toBeDisabled();
  });
});

describe("LlmChip", () => {
  it.each([
    [null, false, /Checking LLM/],
    [{ ...down, available: true, message: "Ollama is ready." }, false, /LLM ready · llama3.2:3b/],
    [down, false, /LLM offline/],
    [null, true, /Service unreachable/],
  ] as const)("shows %#", (status, failed, text) => {
    render(<LlmChip status={status as LlmStatus | null} failed={failed} onRecheck={() => undefined} />);
    expect(screen.getByRole("button")).toHaveTextContent(text);
  });
  it("re-checks on click", async () => {
    const onRecheck = vi.fn();
    render(<LlmChip status={down} failed={false} onRecheck={onRecheck} />);
    await userEvent.click(screen.getByRole("button"));
    expect(onRecheck).toHaveBeenCalled();
  });
});

const phase = (key: PhaseKey, status: Status, extra: Partial<Phase> = {}): Phase =>
  ({ key, title: `Title ${key}`, short: "", describes: "", uses_llm: key === "A", needs_config: key >= "C", review_name: "", status, attempt: 1,
     started_at: null, finished_at: null, error: null, upload_error: null, uploads: [], llm: null, can_continue: false, blocked_reason: null,
     has_summary: false, skipped: false, skip_reason: null, artifacts: [], config: { uploaded: false, valid: false, name: null, table: null, preview: null, error: null }, ...extra }) as Phase;

describe("Stepper", () => {
  const run = (statuses: Status[], complete = false) => ({ complete, phases: (["A", "B", "C", "D"] as PhaseKey[]).map((k, i) => phase(k, statuses[i])) }) as unknown as Run;
  it("locks phases that wait and lets finished ones be opened", async () => {
    const onSelect = vi.fn();
    render(<Stepper run={run(["accepted", "review", "locked", "locked"])} view="B" onSelect={onSelect} />);
    const steps = screen.getAllByRole("button");
    expect(steps[0]).toHaveTextContent("Accepted");
    expect(steps[1]).toHaveAttribute("aria-current", "step");
    expect(steps[2]).toHaveAttribute("aria-disabled", "true");
    await userEvent.click(steps[2]);
    expect(onSelect).not.toHaveBeenCalled();
    await userEvent.click(steps[0]);
    expect(onSelect).toHaveBeenCalledWith("A");
  });
  it("asks for the config where a phase needs one", () => {
    render(<Stepper run={run(["accepted", "accepted", "ready", "locked"])} view="C" onSelect={() => undefined} />);
    expect(screen.getAllByRole("button")[2]).toHaveTextContent("Needs your config file");
  });
  it("opens the results only when the pipeline is complete", async () => {
    const onSelect = vi.fn();
    const { rerender } = render(<Stepper run={run(["accepted", "accepted", "accepted", "review"])} view="D" onSelect={onSelect} />);
    expect(screen.getByRole("button", { name: /Results/ })).toHaveAttribute("aria-disabled", "true");
    rerender(<Stepper run={run(["accepted", "accepted", "accepted", "accepted"], true)} view="D" onSelect={onSelect} />);
    await userEvent.click(screen.getByRole("button", { name: /Results/ }));
    expect(onSelect).toHaveBeenCalledWith("results");
  });
});

describe("LlmBadge", () => {
  it("is honest about what the LLM did", () => {
    const { rerender, container } = render(<LlmBadge phase={phase("A", "review", { llm: { requested: "on", used: true, model: "llama3.2:3b", calls: 12, reason: null } })} />);
    expect(container).toHaveTextContent("LLM · llama3.2:3b · 12 calls");
    rerender(<LlmBadge phase={phase("A", "review", { llm: { requested: "off", used: false, model: null, calls: 0, reason: "chosen" } })} />);
    expect(container).toHaveTextContent("Rule-based fallback · LLM skipped");
    rerender(<LlmBadge phase={phase("A", "review", { llm: { requested: "on", used: false, model: "m", calls: 0, reason: null } })} />);
    expect(container).toHaveTextContent("no questions asked");
    rerender(<LlmBadge phase={phase("C", "review")} />);
    expect(container).toBeEmptyDOMElement();
  });
});

describe("FileDrop", () => {
  it("passes the picked files, with their names as paths", () => {
    const onFiles = vi.fn();
    const { container } = render(<FileDrop accept={[".xlsx"]} multiple title="Drop" onFiles={onFiles} />);
    const input = container.querySelector("input[type=file]") as HTMLInputElement;
    const f = new File(["x"], "a.xlsx");
    fireEvent.change(input, { target: { files: [f] } });
    expect(onFiles).toHaveBeenCalledWith([{ file: f, path: "a.xlsx" }]);
  });
  it("takes only the first file when single-file", () => {
    const onFiles = vi.fn();
    const { container } = render(<FileDrop accept={[".xlsx"]} title="Drop" onFiles={onFiles} />);
    const input = container.querySelector("input[type=file]") as HTMLInputElement;
    fireEvent.change(input, { target: { files: [new File(["1"], "a.xlsx"), new File(["2"], "b.xlsx")] } });
    expect(onFiles.mock.calls[0][0]).toHaveLength(1);
  });
  it("ignores picks and drops while disabled", () => {
    const onFiles = vi.fn();
    render(<FileDrop accept={[".xlsx"]} title="Drop" onFiles={onFiles} disabled />);
    fireEvent.drop(screen.getByRole("button"), { dataTransfer: { items: [], files: [new File(["x"], "a.xlsx")] } });
    expect(onFiles).not.toHaveBeenCalled();
  });
});

describe("DataTable", () => {
  const rows = [
    { code: "N-08", text: "alpha repeats" },
    { code: "N-14", text: "missing week" },
    { code: "N-08", text: "beta repeats" },
  ];
  const columns = [{ key: "code", header: "Code" }, { key: "text", header: "Finding" }];
  it("searches and filters", async () => {
    render(<DataTable rows={rows} columns={columns} filters={[{ label: "N-14", test: (r) => r.code === "N-14" }]} />);
    expect(screen.getAllByRole("row")).toHaveLength(4);
    await userEvent.type(screen.getByLabelText(/Search/), "beta");
    expect(screen.getAllByRole("row")).toHaveLength(2);
    await userEvent.clear(screen.getByLabelText(/Search/));
    await userEvent.click(screen.getByRole("button", { name: "N-14" }));
    expect(screen.getAllByRole("row")).toHaveLength(2);
    expect(screen.getByText(/1 of 3 shown/)).toBeInTheDocument();
  });
  it("says when the workbook holds more than the preview", () => {
    render(<DataTable rows={rows} columns={columns} total={9000} />);
    expect(screen.getByText(/9,000 in total/)).toBeInTheDocument();
  });
  it("shows the empty message", () => {
    render(<DataTable rows={[]} columns={columns} empty="Nothing here." />);
    expect(screen.getByText("Nothing here.")).toBeInTheDocument();
  });
});

describe("small pieces", () => {
  it("lists problems with their location and can expand", async () => {
    const problems = Array.from({ length: 15 }, (_, i) => ({ sheet: "Needs_Review", cell: `B${i + 2}`, message: `bad value ${i}` }));
    render(<ProblemList problems={problems} limit={5} />);
    expect(screen.getAllByRole("listitem")).toHaveLength(6);
    await userEvent.click(screen.getByRole("button", { name: "Show 10 more" }));
    expect(screen.getAllByRole("listitem")).toHaveLength(15);
    expect(screen.getByText("bad value 14")).toBeInTheDocument();
  });
  it.each([["review", "Needs your review"], ["accepted", "Accepted"], ["failed", "Failed"], ["locked", "Locked"]] as const)("labels %s", (s, label) => {
    render(<StatusChip status={s} />);
    expect(screen.getByText(label)).toBeInTheDocument();
  });
});

import { SummaryBView } from "../components/summaries";
import { ErrorBoundary } from "../components/ErrorBoundary";
import type { SummaryB } from "../types";

describe("Phase B results", () => {
  const base: SummaryB = {
    tables: [], totals: { cells_fixed: 1, rows_dropped: 0, needs_review_open: 2 }, unreadable: [], skipped: [],
    by_rule: [{ rule: "D-16", what: "rename", fixed: 0, flagged: 1 }],
    needs_review: [
      { table_ref: "t", row_id: "abc:0:t1:7", column: "Spend", rule: "D-14", why_flagged: "outlier", current_value: "5", original_value: "500", proposed_value: null, severity: "warn", client_report: false, status: "open" },
      { table_ref: "t", row_id: null, column: "fd_burn", rule: "D-16", why_flagged: "cryptic name", current_value: null, original_value: null, proposed_value: null, severity: "warn", client_report: true, status: "open" },
    ],
    needs_review_total: 2,
    fixes: [{ table_ref: "t", row_id: null, column: null, rule: "D-19", action: "dropped", old_value: null, new_value: null, note: "duplicate" }],
    fixes_total: 1, client_questions: 1,
  };
  it("renders items that are about a column (no row id) instead of crashing", () => {
    render(<SummaryBView s={base} />);
    expect(screen.getAllByText("(column)").length).toBeGreaterThan(0);
    expect(screen.getByText("7")).toBeInTheDocument();
    expect(screen.getByText("cryptic name")).toBeInTheDocument();
  });
});

describe("ErrorBoundary", () => {
  it("shows a recoverable message instead of a blank page", () => {
    const Boom = () => { throw new Error("kaboom"); };
    const spy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    render(<ErrorBoundary><Boom /></ErrorBoundary>);
    expect(screen.getByRole("alert")).toHaveTextContent("unexpected problem");
    expect(screen.getByText("kaboom")).toBeInTheDocument();
    spy.mockRestore();
  });
});

import { waitFor } from "@testing-library/react";
import { api } from "../api";
import { ConfigPanel, SkippedPanel } from "../components/panels";

describe("Phase C on data that is not weekly", () => {
  const run = { id: "r1", inputs: [], phases: [] } as unknown as Run;
  const monthly = { ref: "t:0:t1", file: "market.xlsx", sheet: "Sheet1", rows: 72, columns: 6, weekly: false, level: "monthly",
    note: "The data is monthly, not weekly (time column: Year-Month). It has no numeric measure columns to trend or compare." };

  it("says what the data is and offers to skip, instead of an error", async () => {
    vi.spyOn(api, "tables").mockResolvedValue({ tables: [monthly] });
    const skip = vi.spyOn(api, "skip").mockResolvedValue({ ...run } as Run);
    const onChange = vi.fn();
    render(<ConfigPanel run={run} phase={phase("C", "ready")} busy={false} onChange={onChange} onStart={() => undefined} onError={() => undefined} />);
    expect(await screen.findByText("Phase C does not apply to this data")).toBeInTheDocument();
    expect(screen.getByText(/monthly, not weekly/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Skip Phase C and continue" }));
    await waitFor(() => expect(skip).toHaveBeenCalledWith("r1", "C", expect.stringContaining("monthly, not weekly")));
    expect(onChange).toHaveBeenCalled();
  });

  it("lets the analyst set it up anyway", async () => {
    vi.spyOn(api, "tables").mockResolvedValue({ tables: [monthly] });
    render(<ConfigPanel run={run} phase={phase("C", "ready")} busy={false} onChange={vi.fn()} onStart={() => undefined} onError={() => undefined} />);
    await userEvent.click(await screen.findByRole("button", { name: "Set it up anyway" }));
    expect(screen.getByText("Download pre-filled config (.xlsx)")).toBeInTheDocument();
  });

  it("does not interrupt weekly data", async () => {
    vi.spyOn(api, "tables").mockResolvedValue({ tables: [{ ...monthly, weekly: true, level: "weekly", note: "The data is weekly (column 'Date')." }] });
    render(<ConfigPanel run={run} phase={phase("C", "ready")} busy={false} onChange={vi.fn()} onStart={() => undefined} onError={() => undefined} />);
    expect(await screen.findByText("Download pre-filled config (.xlsx)")).toBeInTheDocument();
    expect(screen.queryByText("Phase C does not apply to this data")).not.toBeInTheDocument();
  });

  it("shows why a phase was skipped and the way on", async () => {
    const onGoNext = vi.fn();
    render(<SkippedPanel phase={{ ...phase("C", "accepted"), skipped: true, skip_reason: monthly.note }} nextKey="D" onGoNext={onGoNext} onSetUp={() => undefined} />);
    expect(screen.getByText("Phase C was skipped")).toBeInTheDocument();
    expect(screen.getByText(/monthly, not weekly/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Go to Phase D" }));
    expect(onGoNext).toHaveBeenCalled();
  });
});
