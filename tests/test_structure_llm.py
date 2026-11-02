"""LLM review with a scripted model (spec 10; acceptance criteria 31-34, 47, 48, 50, 53, 54)."""
import json
from pathlib import Path

import pytest

from conftest import LLM_PIN, ScriptedModel, llm_config, read_json, rows, run, simple_sheet, tables, types


def near_tie_csv(tmp_path) -> Path:
    f = tmp_path / "tie.csv"
    f.write_text("a,b,c\nd,e,f\ng,h,i\nj,k,l\n", encoding="utf8")     # text only: rows 1 and 2 nearly tie
    return f


def clear_csv(tmp_path) -> Path:
    f = tmp_path / "clear.csv"
    f.write_text("Date,Channel,Spend\n2025-01-01,TV,10\n2025-01-02,Radio,20\n2025-01-03,Web,35\n", encoding="utf8")
    return f


def go(tmp_path, f, model, cfg=None, **extra):
    return run(tmp_path, [f], cfg or llm_config(), llm=LLM_PIN, llm_transport=model, **extra)


def header_of(out) -> list[str]:
    return [c["name"] for c in json.loads(next(out.glob("files/*/tables/*/columns.json")).read_text())["columns"]]


def decisions(out) -> list[dict]:
    return [json.loads(x) for x in (out / "llm" / "decisions.jsonl").read_text().splitlines()]


def test_unanimous_valid_change_is_used_with_warning(tmp_path):
    model = ScriptedModel({"header_row": "row:2"})
    res, out = go(tmp_path, near_tie_csv(tmp_path), model)
    assert header_of(out) == ["d", "e", "f"]                                   # criterion 33
    d = next(x for x in decisions(out) if x["kind"] == "header_row")
    assert (d["outcome"], d["decided_by"], d["unanimous"]) == ("changed", "llm", True)
    assert types(res)["structure.S-01.uncertain_header"] == "warn"
    assert model.chats == 3                                                    # one question, three votes


def test_two_of_three_wins_only_on_a_near_tie(tmp_path):
    split = lambda options, vote: "row:2" if vote < 2 else "row:1"  # noqa: E731
    res, out = go(tmp_path, near_tie_csv(tmp_path), ScriptedModel({"header_row": split}))
    assert header_of(out) == ["d", "e", "f"]
    res2, out2 = go(tmp_path, clear_csv(tmp_path), ScriptedModel({"header_row": lambda o, v: "row:3" if v < 2 else "row:1"}), out_name="o2")
    d = next(x for x in decisions(out2) if x["kind"] == "header_row")
    assert d["outcome"] == "overruled" and header_of(out2)[0] == "Date"


def test_invalid_or_distant_choices_are_overruled(tmp_path):
    res, out = go(tmp_path, clear_csv(tmp_path), ScriptedModel({"header_row": "row:2"}))   # a data row is not a header
    d = next(x for x in decisions(out) if x["kind"] == "header_row")
    assert d["outcome"] == "overruled" and header_of(out) == ["Date", "Channel", "Spend"]


def test_agreement_raises_no_finding(tmp_path):
    res, out = go(tmp_path, clear_csv(tmp_path), ScriptedModel({"header_row": "row:1"}))
    d = next(x for x in decisions(out) if x["kind"] == "header_row")
    assert (d["outcome"], d["decided_by"]) == ("agreed", "rules+llm")
    assert "structure.S-01.uncertain_header" not in types(res)


def test_failed_votes_count_against_every_choice(tmp_path):
    res, out = go(tmp_path, clear_csv(tmp_path), ScriptedModel({"header_row": "row:1"}, fail_votes={1, 2}))
    d = next(x for x in decisions(out) if x["kind"] == "header_row")
    assert d["majority"] is None and d["outcome"] == "no_majority" and d["votes"] == ["row:1", None, None]


def test_five_failed_calls_stop_the_review(tmp_path):
    two = tmp_path / "two.csv"
    block = ["a,b,c", "d,e,f", "g,h,i"], ["x,y,z", "u,v,w", "q,r,s"]          # two tables, so two header questions
    two.write_text("\n".join(block[0]) + "\n\n\n" + "\n".join(block[1]) + "\n", encoding="utf8")
    model = ScriptedModel({}, fail_votes={0, 1, 2})
    res, out = go(tmp_path, two, model)
    assert types(res)["structure.llm_fallback"] == "info"
    assert read_json(out, "report.json")["extras"]["structure"]["llm"]["fallback"] is True
    assert read_json(out, "report.json")["extras"]["structure"]["llm"]["calls"] == 5   # the sixth is never made
    assert types(res)["structure.llm_fallback"] == "info"          # a failing model never fails the job


def test_server_down_or_remote_equals_rules_only(tmp_path):
    f = clear_csv(tmp_path)
    off, out_off = run(tmp_path, [f], out_name="off")

    def down(path, payload):
        raise OSError("connection refused")
    res, out = go(tmp_path, f, down, out_name="down")
    assert types(res)["structure.llm_fallback"] == "info"
    assert rows(tables(out)[0]) == rows(tables(out_off)[0])                     # criterion 32
    res2, _ = run(tmp_path, [f], llm_config(), out_name="remote", llm={**LLM_PIN}, llm_url="http://10.1.2.3:11434")
    assert types(res2)["structure.llm_fallback"] == "info"


def test_options_are_enums_and_vote_orders_differ(tmp_path):
    seen = []

    class Spy(ScriptedModel):
        def __call__(self, path, payload):
            if path == "/api/chat":
                seen.append(payload["format"]["properties"]["d1"]["properties"]["choice"]["enum"])
            return super().__call__(path, payload)
    go(tmp_path, near_tie_csv(tmp_path), Spy())
    assert len(seen) == 3 and len({tuple(s) for s in seen}) == 3 and len({frozenset(s) for s in seen}) == 1   # criterion 48


def test_review_all_asks_every_table_but_not_excel_tables(make_xlsx, tmp_path):
    from openpyxl.worksheet.table import Table

    def build(wb):
        ws = wb.active
        simple_sheet(ws, row0=1)
        simple_sheet(ws, row0=12, col0=1)
        simple_sheet(wb.create_sheet("Tbl"))
        wb["Tbl"].add_table(Table(displayName="T1", ref="A1:C7"))
    cfg = llm_config()
    res, out = run(tmp_path, [make_xlsx(builder=build)], cfg, llm=LLM_PIN, llm_transport=ScriptedModel())
    kinds = [(d["kind"], d["table_ref"]) for d in decisions(out)]
    assert sorted(t.split(":")[2] for k, t in kinds if k == "header_row") == ["t1", "t2"]     # criterion 47
    near, out2 = run(tmp_path, [make_xlsx("b.xlsx", build)], llm_config(review="near_ties"), out_name="o2",
                     llm=LLM_PIN, llm_transport=ScriptedModel())
    assert not [d for d in decisions(out2) if d["kind"] == "header_row"]


def test_reruns_reuse_stored_answers_without_calling_the_model(tmp_path):
    f = near_tie_csv(tmp_path)
    m1 = ScriptedModel({"header_row": "row:2"})
    r1, o1 = go(tmp_path, f, m1, job_id="j1", out_name="o1")
    m2 = ScriptedModel({"header_row": "row:1"})                                # would answer differently if asked
    r2, o2 = go(tmp_path, f, m2, previous_dir=str(o1), full=True, job_id="j2", out_name="o2")
    assert m2.chats == 0 and header_of(o2) == ["d", "e", "f"]                   # criterion 34
    assert decisions(o2)[0]["reused_from"] == "j1"
    same = ("data.parquet", "columns.json", "cells.parquet", "audit.parquet")
    assert {k: v for k, v in r1["artifacts"].items() if k.endswith(same)} == \
           {k: v for k, v in r2["artifacts"].items() if k.endswith(same)}
    assert [a["id"] for a in r1["action_items"]] == [a["id"] for a in r2["action_items"]]


def test_changed_privacy_asks_again(tmp_path):
    f = clear_csv(tmp_path)
    go(tmp_path, f, ScriptedModel(), out_name="o1", job_id="j1")
    m = ScriptedModel()
    go(tmp_path, f, m, llm_config(privacy="labels_only"), previous_dir=str(tmp_path / "o1"), out_name="o2")
    assert m.chats == 3 and decisions(tmp_path / "o2")[0]["privacy"] == "labels_only"     # criterion 52


def test_correction_is_used_without_the_model_and_raises_info(tmp_path):
    f = near_tie_csv(tmp_path)
    _, o1 = go(tmp_path, f, ScriptedModel(), out_name="o1")
    d = decisions(o1)[0]
    lib = tmp_path / "lib.jsonl"
    lib.write_text(json.dumps({"type": "analyst_correction", "kind": "header_row", "question_hash": d["question_hash"],
                               "grid": d["grid"], "options": d["options"], "right": "row:2", "id": "corr-1"}) + "\n")
    m = ScriptedModel()
    res, out = go(tmp_path, f, m, out_name="o2", examples_path=str(lib))
    assert m.chats == 0 and header_of(out) == ["d", "e", "f"]                   # criterion 54
    d2 = decisions(out)[0]
    assert (d2["outcome"], d2["decided_by"]) == ("corrected", "correction")
    assert types(res)["structure.S-01.uncertain_header"] == "info"


def test_override_writes_a_value_free_correction(tmp_path):
    f = clear_csv(tmp_path)
    _, o1 = go(tmp_path, f, ScriptedModel(), out_name="o1", job_id="j1")
    ref = next(o1.glob("files/*/tables/*")).parent.parent.name + ":0:t1"
    base = json.loads(next(o1.glob("files/*/file.json")).read_text())["sheets"][0]["tables"][0]["layout_hash"]
    cfg = llm_config()
    cfg["structure"]["overrides"] = [{"table": ref, "base": base, "header_rows": [2]}]
    _, o2 = go(tmp_path, f, ScriptedModel(), cfg, out_name="o2", previous_dir=str(o1))
    rec = [json.loads(x) for x in (o2 / "llm" / "examples.jsonl").read_text().splitlines()]
    assert rec and rec[0]["type"] == "analyst_correction" and rec[0]["right"] == "row:2"     # criterion 50
    assert not any(v in json.dumps(rec) for v in ("2025-01-03", "Radio"))       # header labels are structural text, data is not


def test_region_class_change_needs_unanimity_and_keys_survive(make_xlsx, tmp_path):
    def build(wb):
        ws = wb.active
        simple_sheet(ws, row0=1)
        ws["A12"], ws["B12"] = "a", "b"
        for i, (x, y) in enumerate([("c", "d"), ("e", "f"), ("g", "h")], start=13):
            ws.cell(i, 1, x)
            ws.cell(i, 2, y)
    book = make_xlsx(builder=build)
    cfg = llm_config()
    cfg["structure"]["params"] = {"header_min_score": 7.0}
    cfg["structure"]["selection"] = {"s-1": [{"table": "t2"}]}
    frag = ScriptedModel({"region_class": "fragment"})
    res, out = run(tmp_path, [book], cfg, llm=LLM_PIN, llm_transport=frag)
    assert types(res)["structure.selection_not_found"] == "block"               # t2 retired, never reused (criterion 53)
    assert types(res)["structure.assistant_decision"] == "warn"                 # the selector named a retired key
    sheet = read_json(out, next(out.glob("files/*/sheets/00.json")).relative_to(out).as_posix())
    assert [t["table_key"] for t in sheet["tables"]] == ["t1"]
    mixed = ScriptedModel({"region_class": lambda o, v: "fragment" if v < 2 else "table"})
    res2, out2 = run(tmp_path, [book], cfg, llm=LLM_PIN, llm_transport=mixed, out_name="o2")
    sheet2 = read_json(out2, next(out2.glob("files/*/sheets/00.json")).relative_to(out2).as_posix())
    assert [t["table_key"] for t in sheet2["tables"]] == ["t1", "t2"]                 # 2 of 3 never changes a class


def canary_book(wb):
    ws = wb.active
    ws.title = "Media"
    for j, h in enumerate(["hdr_week", "hdr_spend", "hdr_reach"], 1):
        ws.cell(4, j, h)
    for i in range(8):
        for j in range(3):
            ws.cell(5 + i, 1 + j, 7_731_000 + i * 10 + j)
    ws["A2"] = "TITLE_canary"
    ws["C6"].comment = __import__("openpyxl").comments.Comment("COMMENT_canary", "me")


@pytest.mark.parametrize("level", ["structure_only", "labels_only", "full"])
def test_canary_prompts_and_value_free_outputs(make_xlsx, tmp_path, level):
    model = ScriptedModel()
    res, out = run(tmp_path, [make_xlsx(builder=canary_book)], llm_config(privacy=level), llm=LLM_PIN, llm_transport=model)
    prompts = "\n".join(model.prompts)
    assert "COMMENT_canary" not in prompts                                      # criterion 31
    if level == "structure_only":
        assert "hdr_week" not in prompts and "7731" not in prompts and "TITLE_canary" not in prompts
    if level == "labels_only":
        assert "hdr_week" in prompts and "7731" not in prompts and "TITLE_canary" not in prompts
    if level == "full":
        assert "7731" in prompts
    for name in ("report.json", "inventory.json", "llm/decisions.jsonl", "llm/examples.jsonl", "action_items.json"):
        text = (out / name).read_text(encoding="utf8")
        assert "7731" not in text and "TITLE_canary" not in text and "COMMENT_canary" not in text
    for p in out.glob("files/*/sheets/*.json"):
        assert "7731" not in p.read_text(encoding="utf8")
    for p in out.rglob("*"):
        assert p.is_dir() or "TASK structure-choice" not in p.read_bytes().decode("latin-1")   # prompts are never stored


def test_benchmark_scores_assist_against_off(tmp_path):
    from src.structure.llm.benchmark import score
    labelled = tmp_path / "case.csv"
    labelled.write_text("Date,Channel,Spend\n2025-01-01,TV,10\n2025-01-02,Radio,20\n2025-01-03,Web,35\n", encoding="utf8")
    cases = [{"file": str(labelled), "sheet": None, "table": "t1", "header_row": 1, "tables": 1}]
    off = score(cases, None, LLM_PIN)
    assist = score(cases, {}, LLM_PIN, transport=ScriptedModel())
    assert off["header"] == 1.0 and assist["header"] == 1.0 and assist["parse_rate"] == 1.0    # spec 10.9 measures
    assert assist["p95_seconds"] is not None and assist["boundaries"] == 1.0


def test_unpinned_digest_is_recorded_from_the_server(tmp_path):
    model = ScriptedModel(digest="sha256:live")
    run(tmp_path, [near_tie_csv(tmp_path)], llm_config(), llm={**LLM_PIN, "model_digest": None}, llm_transport=model)
    assert {d["model_digest"] for d in decisions(tmp_path / "out")} == {"sha256:live"}
