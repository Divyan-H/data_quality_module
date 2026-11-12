"""The evaluation set as a regression gate (evals/agent/cases.yaml, src/agent/evaluation.py).

These numbers are for the command parser alone: the deterministic, non-agentic baseline that works with no model.
The gate is strict where it protects data (no wrong change, every must-not-change case held) and a floor where it
measures coverage. The local model is evaluated with `python -m src.agent.evaluation --llm on` on a machine that has
Ollama; that run is not part of the automated suite because it needs the model."""
import pytest

from src.agent import evaluation
from src.agent.operations import Plan, RenameColumn, Scale


@pytest.fixture(scope="module")
def report():
    return evaluation.evaluate("off")


def test_the_case_set_is_large_enough_and_covers_both_phases_and_all_tiers():
    cases = evaluation.load_cases()
    assert len(cases) >= 100
    for tier in evaluation.TIERS:
        assert sum(c["tier"] == tier for c in cases) >= 20
    assert {c["phase"] for c in cases} == {"B", "T"}


def test_the_baseline_never_proposes_a_wrong_change(report):
    wrong = [r for r in report["results"] if r["verdict"] == "wrong"]
    assert wrong == [], [(r["id"], r["note"]) for r in wrong]


def test_every_must_not_change_case_is_held(report):
    assert report["summary"]["by_tier"]["adversarial"]["accuracy"] == 1.0


def test_documented_commands_are_understood(report):
    assert report["summary"]["by_tier"]["command"]["accuracy"] >= 0.95           # measured 98.2% on 2026-11-12


def test_free_wording_fails_safely_without_a_model(report):
    block = report["summary"]["by_tier"]["paraphrase"]
    assert block["wrong"] == 0 and block["correct"] + block["safe"] == block["cases"]
    assert report["planner"]["llm"] == "off" and report["summary"]["overall"]["answered_by_model"] == 0


def test_judging_separates_correct_safe_and_wrong():
    case = {"expect": {"ops": ["scale"], "first": {"columns": ["Spend"], "factor": 1000}}}
    assert evaluation.judge(case, Plan(ops=[Scale(columns=["Spend"], factor=1000)]))[0] == "correct"
    assert evaluation.judge(case, Plan(ops=[Scale(columns=["Spend"], factor=100)]))[0] == "wrong"
    assert evaluation.judge(case, Plan(ops=[RenameColumn(column="Spend", new_name="X")]))[0] == "wrong"
    assert evaluation.judge(case, Plan(ask="Which column?"))[0] == "safe"
    assert evaluation.judge({"expect": "no_change"}, Plan(ops=[Scale(columns=["Spend"], factor=2)]))[0] == "wrong"
    assert evaluation.judge({"expect": "no_change"}, Plan(answer="I cannot do that."))[0] == "correct"
    assert evaluation.judge({"expect": "ask"}, Plan(answer="explained"))[0] == "safe"
