"""The agent's core without HTTP or worker processes: the command parser, selectors, every operation, preview / apply
parity, undo, the change-set store, and the LLM planner (with a fake model).

Fixture: a small Phase B output built by running Phase A and B on an in-memory workbook with known values and issues.
"""
import json
import shutil

import openpyxl
import pytest

from src.agent.errors import AgentError, StaleProposal
from src.agent.operations import (AddColumn, Condition, Plan, RemoveRows, RenameColumn, ReplaceText, ReplaceValues,
                                  ResolveFindings, RowFilter, Scale, SetValue)
from src.agent.planner import LlmPlanner, Planner, PlanningContext
from src.agent.planner.parser import parse_rows, split_values
from src.agent.service import AgentService
from src.agent.store import AgentStore
from src.agent.workspace import HygieneWorkspace
from src.hygiene.config import load_config
from src.hygiene.pipeline import run_hygiene
from src.structure.pipeline import run_structure

ROWS = [
    # Date,        City,          Channel, Status,   Spend,   Orders, AOV
    ("2026-01-05", "Bangalore",   "TV",    "Live",   "1,000", "10", "50"),
    ("2026-01-12", "Head Office", "TV",    "Hold",   "2000",  "20", "50"),
    ("2026-01-19", "Mumbai",      "Radio", "Live",   "3000",  "30", "40"),
    ("2026-01-26", "Delhi",       "Print", "Draft",  "4000",  "40", "40"),
    ("2026-02-02", "Bangalore",   "TV",    "Hold",   "5000",  "",   "30"),
    ("2026-02-09", "Mumbai",      "Radio", "Live",   "6000",  "60", "30"),
    ("2026-13-30", "Delhi",       "TV",    "Live",   "7000",  "70", "20"),       # month 13: NULL + a D-02 review item
]


@pytest.fixture(scope="module")
def base_run(tmp_path_factory):
    d = tmp_path_factory.mktemp("agent_core")
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Media"
    ws.append(["Date", "City", "Channel", "Status", "Spend", "Orders", "AOV"])
    for r in ROWS:
        ws.append([None if v == "" else v for v in r])
    src = d / "media.xlsx"
    wb.save(src)
    run_structure({"files": [{"path": str(src), "source_id": "s-1", "file_id": "f-1", "relative_path": "media.xlsx"}],
                   "config": {}, "output_dir": str(d / "a")})
    run_hygiene(str(d / "a"), str(d / "run"), overrides={"fixes": {"D-14": False, "D-27": False}})
    return d / "run"


@pytest.fixture
def run(base_run, tmp_path):
    dst = tmp_path / "run"
    shutil.copytree(base_run, dst)
    return dst


@pytest.fixture
def ws(run):
    return HygieneWorkspace(run, load_config(None))


@pytest.fixture
def ctx(ws):
    return PlanningContext(ws.load())


def column(ws, name):
    t = ws.load()[0]
    return t.data[name]


def plan(ctx, text):
    return Planner(None).plan(text, ctx)


# ── parser helpers ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,rows", [("14", [14]), ("3, 5 and 7", [3, 5, 7]), ("3-5", [3, 4, 5]), ("2 to 4", [2, 3, 4])])
def test_rows_are_read_as_excel_row_numbers(text, rows):
    assert parse_rows(text) == rows


def test_values_split_on_commas_and_words_but_keep_quotes_whole():
    assert split_values('Sedan, Hatchback and 4dr Sedan') == ["Sedan", "Hatchback", "4dr Sedan"]
    assert split_values('"A, B" and "C"') == ["A, B", "C"]


# ── the command parser ─────────────────────────────────────────────────────────
def test_a_value_statement_finds_its_column_from_the_data(ctx):
    p = plan(ctx, "Head Office is Bangalore")
    assert p.ops == [ReplaceValues(mapping={"Head Office": "Bangalore"}, columns=["City"])]


def test_several_values_become_one_bucket(ctx):
    p = plan(ctx, "change TV, Radio and Print to Offline in Channel")
    assert p.ops[0].mapping == {"TV": "Offline", "Radio": "Offline", "Print": "Offline"} and p.ops[0].columns == ["Channel"]


def test_values_can_mean_empty(ctx):
    p = plan(ctx, "Hold and Draft in Status mean empty")
    assert p.ops == [ReplaceValues(mapping={"Hold": None, "Draft": None}, columns=["Status"])]


def test_set_with_a_condition_and_with_rows(ctx):
    assert plan(ctx, "set Channel to Digital where City is Mumbai").ops == [
        SetValue(column="Channel", value="Digital", where=RowFilter(conditions=[Condition(column="City", value="Mumbai")]))]
    assert plan(ctx, "in row 3 set City to Pune").ops == [SetValue(column="City", value="Pune", where=RowFilter(rows=[3]))]
    assert plan(ctx, "make Status Live for rows 2 to 3").ops[0].where.rows == [2, 3]


def test_units_and_division(ctx):
    assert plan(ctx, "Spend is in thousands").ops == [Scale(columns=["Spend"], factor=1000)]
    assert plan(ctx, "divide Spend by 100").ops[0].factor == pytest.approx(0.01)


def test_resolve_by_topic_rows_and_exceptions(ctx):
    op = plan(ctx, "accept all date fixes except row 8").ops[0]
    assert op.decision == "accept" and "D-02" in op.rules and op.where.exclude_rows == [8]
    assert plan(ctx, "keep the original values for the date fixes").ops[0].decision == "reject"


def test_add_column_three_ways(ctx):
    assert plan(ctx, "add column Revenue = Orders * AOV").ops == [AddColumn(name="Revenue", formula="Orders * AOV")]
    assert plan(ctx, "add column Source with value Client").ops == [AddColumn(name="Source", value="Client")]
    op = plan(ctx, "add column Medium from Channel: TV = Offline, Radio = Offline, others = Online").ops[0]
    assert op.mapping == {"TV": "Offline", "Radio": "Offline"} and op.default == "Online" and op.from_column == "Channel"


def test_rename_remove_split_and_text(ctx):
    assert plan(ctx, "rename column AOV to Avg_Order_Value").ops == [RenameColumn(column="AOV", new_name="Avg_Order_Value")]
    assert plan(ctx, "remove rows where Status is Draft").ops == [
        RemoveRows(where=RowFilter(conditions=[Condition(column="Status", value="Draft")]))]
    assert plan(ctx, "remove ' Office' from City").ops == [ReplaceText(column="City", find=" Office")]
    assert plan(ctx, "split the multi-value cells").ops[0].decision == "split"


def test_unknown_names_and_vague_requests_get_a_question(ctx):
    assert "Region" in plan(ctx, "set Region to Delhi").ask
    assert plan(ctx, "set Cityy to Delhi in row 2").ops[0].column == "City"      # a one-letter typo is the column
    assert "Which items" in plan(ctx, "accept").ask
    assert plan(ctx, "Chennai is Madras").answer                       # not a value in the data: not understood
    assert plan(ctx, "Live is Active").ops == [ReplaceValues(mapping={"Live": "Active"}, columns=["Status"])]


def test_help_and_summary_are_answers(ctx):
    assert "preview" in plan(ctx, "help").answer
    assert "open" in plan(ctx, "summary").answer


# ── operations, through preview and apply ─────────────────────────────────────
def test_preview_changes_nothing_and_apply_matches_the_preview(ws):
    before = column(ws, "City")
    ops = [ReplaceValues(mapping={"Head Office": "Bangalore"}, columns=["City"])]
    p = ws.preview(ops, "cs-0001", "x")
    assert p["totals"]["cells"] == 1 and column(ws, "City") == before
    r = ws.apply(ops, "cs-0001", "x", ws.version(), None)
    assert r["totals"] == p["totals"]
    assert column(ws, "City") == [("Bangalore" if v == "Head Office" else v) for v in before]


def test_every_change_is_audited_with_the_changeset(ws, run):
    ws.apply([SetValue(column="Channel", value="Digital", where=RowFilter(rows=[4]))], "cs-0007", "x", ws.version(), None)
    audit = json.loads((run / "phase_b_hygiene" / "remediation_audit.json").read_text())
    mine = [a for a in audit if a["rule"] == "agent"]
    assert len(mine) == 1 and mine[0]["changeset"] == "cs-0007" and mine[0]["old_value"] == "Radio" and mine[0]["source_row"] == 4


def test_scale_keeps_exact_decimals_and_skips_non_numbers(ws):
    ws.apply([Scale(columns=["Spend"], factor=0.001)], "cs-0001", "x", ws.version(), None)
    assert column(ws, "Spend")[:2] == ["1", "2"]


def test_formula_column_leaves_missing_inputs_empty(ws):
    p = ws.preview([AddColumn(name="Revenue", formula="Orders * AOV")], "cs-0001", "x")
    assert "1 row(s) get an empty cell" in " ".join(p["operations"][0]["effect"]["notes"])
    ws.apply([AddColumn(name="Revenue", formula="Orders * AOV")], "cs-0001", "x", ws.version(), None)
    assert column(ws, "Revenue")[:5] == ["500", "1000", "1200", "1600", None]


def test_formula_rejects_anything_but_arithmetic(ws):
    with pytest.raises(AgentError):
        ws.preview([AddColumn(name="X", formula="__import__('os').system('x')")], "cs-0001", "x")


def test_remove_rows_never_removes_everything_and_closes_their_items(ws):
    with pytest.raises(AgentError, match="every row"):
        ws.preview([RemoveRows(where=RowFilter(conditions=[Condition(column="City", op="not_empty")]))], "cs-0001", "x")
    p = ws.preview([RemoveRows(where=RowFilter(conditions=[Condition(column="Status", value="Draft")]))], "cs-0001", "x")
    assert p["totals"]["rows_removed"] == 1


def test_mapping_reports_values_it_could_not_find(ws):
    p = ws.preview([ReplaceValues(mapping={"Chennai": "Madras", "TV": "Television"}, columns=["Channel", "City"])], "cs-0001", "x")
    assert p["operations"][0]["effect"]["unmatched"] == ["Chennai"]


def test_resolving_items_writes_the_decision_like_the_workbook(ws):
    items = [r for t in ws.load() for r in t.review if r["status"] == "open"]
    p = ws.preview([ResolveFindings(decision="keep")], "cs-0001", "x")
    assert p["totals"]["items_resolved"] == len(items) and p["open_items_after"] == 0


def test_a_stale_proposal_is_refused(ws):
    ops = [SetValue(column="City", value="X", where=RowFilter(rows=[2]))]
    v = ws.version()
    ws.apply(ops, "cs-0001", "x", v, None)
    with pytest.raises(StaleProposal):
        ws.apply(ops, "cs-0002", "x", v, None)


def test_rename_carries_audit_and_types(ws):
    ws.apply([RenameColumn(column="AOV", new_name="Avg_Order_Value")], "cs-0001", "x", ws.version(), None)
    t = ws.load()[0]
    assert "Avg_Order_Value" in t.cols and "AOV" not in t.cols and t.types["Avg_Order_Value"] == "numeric"


# ── the service: conversation, change-sets, undo ──────────────────────────────
def test_service_round_trip_with_undo(run):
    svc = AgentService(HygieneWorkspace(run, load_config(None)), AgentStore(run, "B"), Planner(None))
    before = column(svc.ws, "City")
    cs = svc.message("Head Office is Bangalore")["changeset"]
    assert cs["status"] == "proposed"
    other = svc.message("Hold in Status means empty")["changeset"]["id"]
    svc.apply(cs["id"])
    assert svc.store.get(other).status == "stale"
    assert "Head Office" not in column(svc.ws, "City")
    svc.undo()
    assert column(svc.ws, "City") == before
    assert svc.store.get(cs["id"]).status == "undone"
    with pytest.raises(AgentError):
        svc.undo()
    kinds = [m.kind for m in svc.store.messages()]
    assert kinds == ["text", "proposal", "text", "proposal", "result", "result"]


def test_a_message_that_changes_nothing_creates_no_changeset(run):
    svc = AgentService(HygieneWorkspace(run, load_config(None)), AgentStore(run, "B"), Planner(None))
    r = svc.message("set City to Delhi where City is Nowhere")
    assert r["changeset"] is None and "Nothing would change" in r["messages"][1]["text"]
    assert svc.store.all() == []


# ── the LLM planner, with a fake model ────────────────────────────────────────
class FakeClient:
    def __init__(self, answer, ok=True):
        self.answer, self.ok, self.calls, self.unavailable_reason = answer, ok, [], None if ok else "llm_unreachable"

    def check(self):
        return self.ok

    def chat(self, prompt, schema, seed, temperature, **kw):
        self.calls.append({"prompt": prompt, "schema": schema, **kw})
        return self.answer


def test_the_model_is_asked_only_when_the_parser_cannot_read_the_instruction(ctx):
    fake = FakeClient({"intent": "change", "ops": [{"op": "set_value", "column": "Status", "value": "Live",
                                                    "conditions": [{"column": "City", "op": "eq", "value": "Delhi"}]}]})
    planner = Planner(LlmPlanner(fake))
    assert planner.plan("Head Office is Bangalore", ctx).source == "rules" and not fake.calls
    p = planner.plan("every Delhi row is actually live", ctx)
    assert p.source == "llm" and p.ops == [SetValue(column="Status", value="Live", where=RowFilter(
        conditions=[Condition(column="City", value="Delhi")]))]
    schema = fake.calls[0]["schema"]
    assert "City" in schema["properties"]["ops"]["items"]["properties"]["column"]["enum"]
    assert '"Head Office"' in fake.calls[0]["prompt"] or "Head Office" in fake.calls[0]["prompt"]


def test_an_invalid_model_answer_becomes_a_question_not_a_change(ctx):
    bad = Planner(LlmPlanner(FakeClient({"intent": "change", "ops": [{"op": "set_value", "column": "Nope", "value": "x"}]})))
    assert bad.plan("do the thing to the stuff", ctx).ask
    unclear = Planner(LlmPlanner(FakeClient({"intent": "unclear", "ask": "Which column?"})))
    assert unclear.plan("do the thing", ctx).ask == "Which column?"
    question = Planner(LlmPlanner(FakeClient({"intent": "question"})))
    assert "open" in question.plan("what's wrong with my data please", ctx).answer


def test_without_a_model_free_wording_is_explained(ctx):
    p = Planner(LlmPlanner(FakeClient(None, ok=False))).plan("do the thing to the stuff", ctx)
    assert p.answer and "not available" in p.answer and not p.ops


def test_a_plan_has_exactly_one_kind():
    with pytest.raises(ValueError):
        Plan(ask="a", answer="b")


# ── shape-safe additions in Phase B: split a column, if / else ────────────────
def test_split_column_and_classify_in_phase_b(ws):
    from src.agent.operations import Classify, Rule, SplitColumn
    ws.apply([SplitColumn(column="Date", into=["Year", "Month", "Day"], separator="-"),
              Classify(column="Size", rules=[Rule(when=[Condition(column="Spend", op="gt", value=4000)], value="Big")], default="Small")],
             "cs-0001", "x", ws.version(), None)
    t = ws.load()[0]
    assert t.data["Year"][:2] == ["2026", "2026"] and t.data["Month"][0] == "1"      # new columns get Phase B's rules: typed
    assert t.data["Size"] == ["Small", "Small", "Small", "Small", "Big", "Big", "Big"]


def test_reshaping_in_phase_b_points_to_phase_t(ws):
    from src.agent.operations import Unpivot
    with pytest.raises(AgentError, match="Phase T"):
        ws.preview([Unpivot(columns=["Orders", "AOV"])], "cs-0001", "x")
