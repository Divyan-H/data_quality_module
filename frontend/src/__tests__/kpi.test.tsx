import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { StartPanel } from "../components/panels";
import { SummaryBView, SummaryDView } from "../components/summaries";
import type { ColumnChoiceTable, Phase, Run, ServiceOptions, SummaryB, SummaryD } from "../types";

const reply = (body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }));
afterEach(() => vi.unstubAllGlobals());

const choice: ColumnChoiceTable[] = [
  {
    ref: "f:0:t1", file: "daily.xlsx", sheet: "Sheet1", rows: 40, dated: true, period: "Date",
    columns: [
      { name: "Sales", numbers: 40, zeros: 1, blanks: 0, zero_share: 0.025, likely_zero_valid: false, name_hint: false },
      { name: "Rain", numbers: 40, zeros: 30, blanks: 0, zero_share: 0.75, likely_zero_valid: true, name_hint: true },
    ],
  },
  { ref: "f:1:t1", file: "daily.xlsx", sheet: "Notes", rows: 5, dated: false, period: null, columns: [] },
];

const options = { llm: { model: "m", review: "all" }, hygiene_fixes: [{ id: "D-14", what: "caps", default: false }, { id: "D-27", what: "fills", default: false }] } as unknown as ServiceOptions;
const phaseB = { key: "B", title: "Cell-level hygiene", short: "", describes: "", status: "ready", error: null, skipped: false, artifacts: [], config: {} } as unknown as Phase;
const run = { id: "r1", inputs: [] } as unknown as Run;

describe("Phase B start screen: zero is a valid value", () => {
  it("lists the numeric columns of dated tables, pre-ticks mostly-zero ones and sends the ticked columns", async () => {
    vi.stubGlobal("fetch", vi.fn(() => reply({ tables: choice })));
    const onStart = vi.fn();
    render(<StartPanel run={run} phase={phaseB} options={options} busy={false} onStart={onStart} llmOffline={false} />);
    const rain = await screen.findByRole("checkbox", { name: /Rain/ });
    const sales = screen.getByRole("checkbox", { name: /Sales/ });
    expect(rain).toBeChecked();                                           // 30 of 40 zeros: content says zero is normal here
    expect(sales).not.toBeChecked();
    expect(screen.getByText(/no date column, so D-27 does not apply/)).toBeInTheDocument();
    await userEvent.click(sales);
    await userEvent.click(rain);
    await userEvent.click(screen.getByRole("button", { name: "Run Phase B" }));
    expect(onStart).toHaveBeenCalledWith({ fixes: { "D-14": false, "D-27": false }, zero_expected: { "f:0:t1": ["Sales"] } });
  });

  it("sends no zero_expected when nothing is ticked, and says when the columns cannot be read", async () => {
    vi.stubGlobal("fetch", vi.fn(() => Promise.resolve(new Response(JSON.stringify({ error: { code: "locked", message: "Accept Phase A first." } }), { status: 409 }))));
    const onStart = vi.fn();
    render(<StartPanel run={run} phase={phaseB} options={options} busy={false} onStart={onStart} llmOffline={false} />);
    await waitFor(() => expect(screen.getByText("Accept Phase A first.")).toBeInTheDocument());
    await userEvent.click(screen.getByRole("button", { name: "Run Phase B" }));
    expect(onStart).toHaveBeenCalledWith({ fixes: { "D-14": false, "D-27": false } });
  });
});

describe("summaries", () => {
  const b = {
    tables: [], totals: { cells_fixed: 0, rows_dropped: 0, needs_review_open: 0 }, unreadable: [], skipped: [], by_rule: [], needs_review: [],
    needs_review_total: 0, fixes: [], fixes_total: 0, client_questions: 0,
  } as unknown as SummaryB;

  it("shows what adds up in Phase B and what to leave out of a sum", () => {
    render(<SummaryBView s={{
      ...b, zero_expected: { "f:0:t1": ["Rain"] },
      buckets: [{ table_ref: "f:0:t1", sheet: "Sheet1", where: "columns", label_column: null, bucket: "Bucket 1", sum_these: ["Search", "Social"],
                  do_not_add_with_them: ["All media"], status: "open", relations: [] }],
    }} />);
    expect(screen.getByText("Columns that add up (double-counting risk)")).toBeInTheDocument();
    expect(screen.getByText("Search, Social")).toBeInTheDocument();
    expect(screen.getByText("All media")).toBeInTheDocument();
    expect(screen.getByText(/Zero kept as a valid value.*Rain/)).toBeInTheDocument();
  });

  it("shows nothing about buckets or weeks when there are none", () => {
    render(<SummaryBView s={b} />);
    expect(screen.queryByText(/Columns that add up/)).toBeNull();
    render(<SummaryDView s={{ tables: [], not_checked: [] } as unknown as SummaryD} />);
    expect(screen.queryByText(/spread over weeks/)).toBeNull();
  });

  it("says that monthly data was also spread over weeks", () => {
    render(<SummaryDView s={{
      tables: [], not_checked: [],
      weekly: [{ table_ref: "m:0:t1", source_file: "m.xlsx", sheet: "Sheet1", rows_in: 72, rows_out: 330, measures: ["Sales"], exact: 72, unreadable: 0, inexact: [] }],
    } as unknown as SummaryD} />);
    expect(screen.getByText("Monthly data was also spread over weeks")).toBeInTheDocument();
    expect(screen.getByText(/72 monthly rows became 330 weekly rows/)).toBeInTheDocument();
  });
});
