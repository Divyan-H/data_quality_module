import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ChatPanel, ProposalCard } from "../components/agent/ChatPanel";
import { IssueList } from "../components/agent/IssueList";
import { dataVersion, statusLabel, suggestionsFor } from "../components/agent/logic";
import type { AgentState, ChangeSetView, IssueGroup, IssuesView, Phase } from "../types";

const group: IssueGroup = {
  table: "t1", rule: "D-14", what: "Outlier capped to the nearest normal value", column: "Radio_GRPs", count: 3, cells: 3,
  severity: "warn", client: false, why: "Far above the series", suggested: "Accept or reject", examples: ["676.67"], rows: [5, 9],
};

const preview = {
  operations: [{
    text: "Accept the proposed fix for every open review item of D-14 in Radio_GRPs.",
    summary: "1 cell changed, 1 review item closed.",
    effect: {
      cells: 1, items_resolved: 1, rows_removed: 0, rows_added: 0, columns_added: [], columns_renamed: [], tables: ["t1"],
      samples: [{ table: "t1", file: "weekly.xlsx", sheet: "Data", excel_row: 9, row_id: "t1:9", column: "Radio_GRPs", old: "676.67", new: "220" }],
      unmatched: [], unmatched_total: 0, notes: [],
    },
  }],
  totals: { cells: 1, items_resolved: 1, rows_removed: 0, rows_added: 0, columns_added: [], columns_renamed: [], tables: ["t1"] },
  summary: "1 cell changed, 1 review item closed.", items_answered_by_edits: 0, follow_up_fixes: 0, open_items_after: 2,
  changes_anything: true,
};

const cs = (status: ChangeSetView["status"]): ChangeSetView => ({
  id: "cs-0001", status, instruction: "accept the outlier fixes in Radio_GRPs", source: "rules", summary: preview.summary,
  created_at: "2026-11-02T10:00:00Z", applied_at: null, undone_at: null, error: null, preview,
});

const state = (over: Partial<AgentState> = {}): AgentState => ({
  phase: "B", status: "review", usable: true, working: false, job_action: null,
  messages: [
    { id: 1, role: "user", kind: "text", text: "accept the outlier fixes in Radio_GRPs", changeset: null, at: "" },
    { id: 2, role: "agent", kind: "proposal", text: "Here is what I would change.", changeset: "cs-0001", at: "" },
  ],
  changesets: { "cs-0001": cs("proposed") }, undo: null, error: null,
  llm: { enabled: false, available: false, model: null, message: "commands only" }, ...over,
});

describe("agent logic", () => {
  it("offers answers that fit the rule", () => {
    expect(suggestionsFor(group).map((s) => s.text)).toEqual([
      "accept the D-14 items in Radio_GRPs", "keep the D-14 items in Radio_GRPs", "reject the D-14 items in Radio_GRPs"]);
    expect(suggestionsFor({ ...group, rule: "D-29", column: "City" }).map((s) => s.label)).toContain("Split rows");
  });
  it("labels every change-set status", () => {
    for (const s of ["proposed", "applied", "discarded", "undone", "stale", "failed"] as const) expect(statusLabel(cs(s)).text).toBeTruthy();
  });
  it("changes the data version when an agent change is applied", () => {
    const p = { status: "review", attempt: 1, finished_at: "x", uploads: [], agent: null } as unknown as Phase;
    expect(dataVersion(p)).not.toEqual(dataVersion({ ...p, agent: { applied: 1, undone: 0, last_at: "t" } }));
  });
});

describe("IssueList", () => {
  const issues: IssuesView = { open: 3, resolved: 0, resolved_by_agent: 0, groups: [group], tables: [] };
  it("selects a group and turns its chips into chat text", async () => {
    const onSelect = vi.fn();
    const onSuggest = vi.fn();
    const { rerender } = render(<IssueList issues={issues} selected={null} onSelect={onSelect} onSuggest={onSuggest} disabled={false} />);
    await userEvent.click(screen.getByRole("button", { name: /D-14/ }));
    expect(onSelect).toHaveBeenCalledWith(group);
    rerender(<IssueList issues={issues} selected="t1|D-14|Radio_GRPs" onSelect={onSelect} onSuggest={onSuggest} disabled={false} />);
    await userEvent.click(screen.getByRole("button", { name: "Accept" }));
    expect(onSuggest).toHaveBeenCalledWith("accept the D-14 items in Radio_GRPs");
  });
  it("says so when nothing is open", () => {
    render(<IssueList issues={{ ...issues, open: 0, groups: [] }} selected={null} onSelect={vi.fn()} onSuggest={vi.fn()} disabled={false} />);
    expect(screen.getByText(/Nothing is open/)).toBeInTheDocument();
  });
});

describe("ProposalCard", () => {
  it("shows the plan, a before/after sample, and Apply / Discard while proposed", async () => {
    const f = { onApply: vi.fn(), onDiscard: vi.fn(), onAgain: vi.fn() };
    render(<ProposalCard cs={cs("proposed")} busy={false} {...f} />);
    const card = screen.getByLabelText("Change cs-0001");
    expect(within(card).getByText(/Accept the proposed fix/)).toBeInTheDocument();
    expect(within(card).getByText("676.67")).toBeInTheDocument();
    expect(within(card).getByText("220")).toBeInTheDocument();
    await userEvent.click(within(card).getByRole("button", { name: "Apply" }));
    await userEvent.click(within(card).getByRole("button", { name: "Discard" }));
    expect(f.onApply).toHaveBeenCalledOnce();
    expect(f.onDiscard).toHaveBeenCalledOnce();
  });
  it("offers a fresh preview for an out-of-date proposal and no Apply", async () => {
    const f = { onApply: vi.fn(), onDiscard: vi.fn(), onAgain: vi.fn() };
    render(<ProposalCard cs={cs("stale")} busy={false} {...f} />);
    expect(screen.queryByRole("button", { name: "Apply" })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Preview again" }));
    expect(f.onAgain).toHaveBeenCalledOnce();
  });
});

describe("ChatPanel", () => {
  const setup = (over: Partial<Parameters<typeof ChatPanel>[0]> = {}) => {
    const props = {
      state: state(), busy: false, sending: false, working: null, draft: "", setDraft: vi.fn(), onSend: vi.fn(),
      onApply: vi.fn(), onDiscard: vi.fn(), onUndo: vi.fn(), ...over,
    };
    render(<ChatPanel {...props} />);
    return props;
  };
  it("renders the conversation with the proposal card inline", () => {
    setup();
    expect(screen.getByText("accept the outlier fixes in Radio_GRPs", { selector: ".msg-text" })).toBeInTheDocument();
    expect(screen.getByLabelText("Change cs-0001")).toBeInTheDocument();
  });
  it("sends on Enter and not on Shift+Enter", async () => {
    const p = setup({ draft: "Head Office is Bangalore" });
    const box = screen.getByLabelText("Instruction for the agent");
    await userEvent.type(box, "{Shift>}{Enter}{/Shift}");
    expect(p.onSend).not.toHaveBeenCalled();
    await userEvent.type(box, "{Enter}");
    expect(p.onSend).toHaveBeenCalledWith("Head Office is Bangalore");
  });
  it("offers Undo for the last applied change", async () => {
    const applied = { ...cs("applied") } as Omit<ChangeSetView, "preview"> & { preview?: unknown };
    delete applied.preview;
    const p = setup({ state: state({ undo: applied as Omit<ChangeSetView, "preview"> }) });
    await userEvent.click(screen.getByRole("button", { name: "Undo last change" }));
    expect(p.onUndo).toHaveBeenCalledOnce();
  });
  it("locks input and buttons while a change is being applied", () => {
    setup({ busy: true, working: "Applying the change…", draft: "x" });
    expect(screen.getByText("Applying the change…")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Apply" })).toBeDisabled();
  });
});

describe("Phase T", () => {
  const tPreview = {
    kind: "transform" as const,
    operations: [{
      text: "Turn Day 1, Day 2 into rows: column names into 'Day', values into 'Value'.",
      summary: "Reactivation: 2 → 4 rows, +Day, +Value; −Day 1, −Day 2",
      effect: {
        tables: [{ table: "Reactivation", rows_before: 2, rows_after: 4, columns_added: ["Day", "Value"], columns_removed: ["Day 1", "Day 2"] }],
        cells: 0, notes: ["2 column(s) turned into rows: Day 1, Day 2."], unmatched: [], unmatched_total: 0,
        sample: { table: "Reactivation", columns: ["City", "Day", "Value"], rows: [["Pune", "Day 1", "1"], ["Pune", "Day 2", "2"]] },
      },
    }],
    summary: "Reactivation: 2 → 4 rows", steps_before: 0, tables_after: [{ name: "Reactivation", rows: 4, columns: 3 }],
    changes_anything: true,
  };
  it("previews a reshaping step with the first rows of the result", async () => {
    const f = { onApply: vi.fn(), onDiscard: vi.fn(), onAgain: vi.fn() };
    render(<ProposalCard cs={{ ...cs("proposed"), preview: tPreview }} busy={false} {...f} />);
    expect(screen.getByText(/2 → 4 rows/)).toBeInTheDocument();
    expect(screen.getAllByText("Pune")).toHaveLength(2);
    await userEvent.click(screen.getByRole("button", { name: "Apply and add to the recipe" }));
    expect(f.onApply).toHaveBeenCalledOnce();
  });
  it("lists the recipe steps, a failing step, and the output tables", async () => {
    const onSuggest = vi.fn();
    const { RecipePanel } = await import("../components/agent/RecipePanel");
    render(<RecipePanel disabled={false} onSuggest={onSuggest} issues={{
      open: 0, resolved: 0, resolved_by_agent: 0, groups: [],
      tables: [{ ref: "Reactivation", file: null, sheet: "Reactivation", rows: 4, columns: ["City", "Day", "Value"] }],
      steps: [{ step: 1, text: "Turn Day 1, Day 2 into rows.", summary: "2 → 4 rows", changeset: "cs-0001", notes: [] }],
      failed: { step: 2, text: "Drop Nope", message: "There is no column 'Nope'." },
    }} />);
    expect(screen.getByRole("list", { name: "Recipe steps" })).toHaveTextContent("Turn Day 1, Day 2 into rows.");
    expect(screen.getByText(/Step 2 does not run/)).toBeInTheDocument();
    expect(screen.getByText(/4 rows · 3 cols/)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "show recipe" }));
    expect(onSuggest).toHaveBeenCalledWith("show recipe");
  });
});

describe("guardrail confirmation", () => {
  const elevated: ChangeSetView = { ...cs("proposed"), review: { level: "elevated", reasons: ["It removes rows."] } };

  it("keeps Apply disabled until the analyst ticks the confirmation, and shows why", async () => {
    const onApply = vi.fn();
    render(<ProposalCard cs={elevated} busy={false} onApply={onApply} onDiscard={() => {}} onAgain={() => {}} />);
    const apply = screen.getByRole("button", { name: "Apply" });
    expect(apply).toBeDisabled();
    expect(screen.getByText("It removes rows.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("checkbox"));
    expect(apply).toBeEnabled();
    await userEvent.click(apply);
    expect(onApply).toHaveBeenCalledTimes(1);
  });

  it("asks for nothing extra on an ordinary change", () => {
    render(<ProposalCard cs={{ ...cs("proposed"), review: { level: "normal", reasons: [] } }} busy={false} onApply={() => {}} onDiscard={() => {}} onAgain={() => {}} />);
    expect(screen.queryByRole("checkbox")).toBeNull();
    expect(screen.getByRole("button", { name: "Apply" })).toBeEnabled();
  });
});
