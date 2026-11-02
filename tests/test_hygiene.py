"""Phase B (src/hygiene) tests. Tables are built in memory from dirty rows; nothing is committed."""
import json
from datetime import date
from pathlib import Path

import openpyxl
import polars as pl
import pytest

from src.hygiene.config import load_config
from src.hygiene.pipeline import OUT, clean, run_hygiene
from src.hygiene.review import ReviewError, apply_upload
from src.hygiene.table import Table
from src.hygiene.values import parse_date, parse_number


def make(cols: dict, fixes=None, unique=True) -> Table:
    """A Table from {column: [text values]}; Row_IDs are f:0:t1:<n>. `unique` adds a counter column so that
    rows that look alike are not exact duplicates (D-19 would drop them)."""
    n = len(next(iter(cols.values())))
    if unique:
        cols = {**cols, "Line No": [str(i) for i in range(n)]}
    data = {"_row_id": [f"f:0:t1:{i + 1}" for i in range(n)], "_source_row": list(range(1, n + 1))}
    return Table("f:0:t1", {**data, **cols}, load_config(overrides={"fixes": fixes} if fixes else None))


def rules(t, code):
    return [r for r in t.review if r["rule"] == code]


def audited(t, code):
    return [(a["row_id"], a["old_value"], a["new_value"]) for a in t.audit if a["rule"] == code]


# ── value helpers ────────────────────────────────────────────────────────────
def test_locale_aware_numbers():
    assert parse_number("$1,234.50") == "1234.50"
    assert parse_number("€99,99") == "99.99"            # never 9999
    assert parse_number("1.234,56") == "1234.56"
    assert parse_number("1,234") == "1234" and parse_number("1,234", eu=True) == "1.234"
    assert parse_number("(12)") == "-12" and parse_number("abc") is None and parse_number("1,23,4") is None


def test_dates_clamp_and_invalid():
    assert parse_date("30/02/2026", True, 1900, 2100)[:2] == ("clamp", (2026, 2, 28))
    assert parse_date("2026-13-01", True, 1900, 2100)[0] == "bad_month"
    assert parse_date("12/13/2026", True, 1900, 2100)[:2] == ("ok", (2026, 12, 13))


# ── noise ────────────────────────────────────────────────────────────────────
def test_noise_whitespace_control_blank_errors_encoding():
    t = clean(make({"Customer Name ": ["  Ann  Lee ", "CafÃ©", "Bo\x00b", "", "#N/A", "#REF!"]}))
    assert "Customer Name" in t.data                                  # D-11 header trimmed
    assert t.data["Customer Name"] == ["Ann Lee", "Café", "Bo b", None, None, None]
    assert {a["rule"] for a in t.audit} >= {"D-09", "D-11", "D-13", "D-20", "D-07", "D-08", "N-02"}


def test_null_placeholders_respect_dominant_value():
    t = clean(make({"Status": ["Pending"] * 40 + ["Done"] * 5 + ["NA", "N/A", "null", "None", "-"]}))
    assert t.data["Status"].count("Pending") == 40                    # legitimate value, untouched
    assert t.data["Status"][-5:] == [None] * 5
    t2 = clean(make({"Region": ["NA"] * 2 + ["EU"] * 30}))            # 'NA' = North America in a region column
    assert t2.data["Region"].count("NA") == 2


# ── dates ────────────────────────────────────────────────────────────────────
def test_dates_formats_serials_invalid():
    t = clean(make({"Order Date": ["2026-01-05", "06/01/2026", "30/02/2026", "2026-13-01", "45292", "07 Jan 2026", "2026-01-08"]}))
    col = t.data["Order Date"]
    assert col[1] == "2026-01-06" and col[5] == "2026-01-07"          # D-03 to the dominant (ISO) format
    assert col[2] == "2026-02-28"                                     # D-02 clamp, no review needed
    assert col[3] is None and rules(t, "D-02")[0]["original_value"] == "2026-13-01"
    assert col[4] == "2024-01-01"                                     # D-25 serial
    assert t.types["Order Date"] == "date"


def test_number_misread_as_date_becomes_serial():
    t = clean(make({"Clicks": ["10", "20", "30", "2026-01-05", "40", "50"]}))
    assert t.data["Clicks"][3] == "46027" and audited(t, "D-01")


# ── numbers ──────────────────────────────────────────────────────────────────
def test_currency_and_mixed_types():
    t = clean(make({"Spend": ["$1,234.50", "€99,99", "100", "abc", "50", "60", "70"]}))
    assert t.data["Spend"] == ["1234.50", "99.99", "100", None, "50", "60", "70"]
    assert rules(t, "D-04")[0]["original_value"] == "abc"             # failed conversion -> review queue
    assert len(audited(t, "E-01")) == 2


def test_text_dominant_and_id_columns_untouched():
    t = clean(make({"Account_Number": ["A1", "A2", "A3", "A4", "5"], "Notes": ["x", "y", "5", "z", "w"]}))
    assert not rules(t, "D-04") and t.data["Notes"][2] == "5"


def test_leading_zeros_padded():
    t = clean(make({"ZIP_Code": ["02134", "10001", "2134", "90210", "7001.0"]}))
    assert t.data["ZIP_Code"] == ["02134", "10001", "02134", "90210", "07001"]


# ── rows ─────────────────────────────────────────────────────────────────────
def test_exact_duplicates_dropped_and_same_key_reviewed():
    ids = [f"INV-{i}" for i in range(40)] + ["INV-0", "INV-1"]          # INV-0 exact duplicate, INV-1 same key
    amts = [str(i) for i in range(40)] + ["0", "999"]
    t = clean(make({"Invoice_ID": ids, "Amt": amts}, unique=False))
    assert len(t.data["_row_id"]) == 41 and "f:0:t1:41" not in t.data["_row_id"]
    drop = [a for a in t.audit if a["action"] == "dropped"]
    assert len(drop) == 1 and drop[0]["row_id"] == "f:0:t1:41" and drop[0]["old_value"] == "f:0:t1:1"
    assert [r["row_id"] for r in rules(t, "D-19")] == ["f:0:t1:42"]  # same key INV-1, different amount


def test_repeated_id_column_is_not_a_key_and_ids_are_not_padded():
    t = clean(make({"vehicle_id": ["1", "8", "97", "97", "12", "8"], "Spend": ["1", "2", "3", "4", "5", "6"]}))
    assert not rules(t, "D-19") and t.data["vehicle_id"][:2] == ["1", "8"]


def test_half_numeric_column_is_flagged_not_nulled():
    vals = ["97", "1144", "HCV - 9ft", "Vehicle IDs: 3, 11", "128", "LCV", "2", "Tata Ace"]
    t = clean(make({"Vehicle Model / Category": vals}))
    assert t.data["Vehicle Model / Category"] == vals and rules(t, "D-04")[0]["scope"] == "column"


def test_case_normalised_to_dominant():
    t = clean(make({"Brand": ["Apple"] * 5 + ["apple"] * 2 + ["APPLE", "Pear"]}))
    assert set(t.data["Brand"]) == {"Apple", "Pear"}


# ── flag-only checks ─────────────────────────────────────────────────────────
def test_outliers_capped_and_reviewable():
    t = clean(make({"Clicks": [str(x) for x in (100, 102, 98, 101, 99, 103, 97, 100, 50000)]}))
    assert t.data["Clicks"][-1] == "103"                             # capped to the largest normal value
    item = rules(t, "D-14")[0]
    assert item["row_id"] == "f:0:t1:9" and item["original_value"] == "50000" and item["current_value"] == "103"


def test_minus_signs_units_and_cryptic_names():
    t = clean(make({"Loss": ["5", "-6", "7", "8"] + [None] * 5,
                    "Revenue": ["5", "6", "7", "8"] + [None] * 5,
                    "Pct": ["0.1", "0.2", "5", "0.4", "0.3", "0.25"] + [None] * 3,
                    "Spend": ["10", "12", "11", "1300", "9", "10", "13", "12", "11"],
                    "col_7": list("abcdefghi"), "fd_burn": list("abcdefghi"), "vehicle_id": list("abcdefghi")}))
    assert t.data["Loss"][:4] == ["5", "-6", "7", "8"]                # D-15 flags only: no value is changed
    assert [(r["scope"], r["column"]) for r in rules(t, "D-15")] == [("column", "Loss")]   # one item, only for the column with a negative
    assert "1 negative value(s)" in rules(t, "D-15")[0]["why_flagged"]
    assert t.data["Pct"][2] == "0.05"                                 # D-22: majority are fractions
    assert t.data["Spend"][3] == "13"                                 # D-22: cents among dollars
    assert sorted(r["column"] for r in rules(t, "D-16")) == ["col_7", "fd_burn"]     # vehicle_id is a normal name


def test_meaning_changing_fixes_can_be_switched_off():
    t = clean(make({"Clicks": ["100", "102", "98", "101", "99", "103", "97", "100", "50000"]}, fixes={"D-14": False}))
    assert t.data["Clicks"][-1] == "50000" and rules(t, "D-14")[0]["proposed_value"] == "103"


def test_near_duplicates_merged_sentinels_nulled_precision_text():
    t = clean(make({"Vendor": ["Acme Corp"] * 4 + ["Acme Corp."] * 2 + ["Zed"] * 3,
                    "Budget": ["9999", "10", "11", "12"] * 2 + ["13"],
                    "Phone": [f"9{i}00011111" for i in range(9)]}))
    assert set(t.data["Vendor"]) == {"Acme Corp", "Zed"}               # D-12 merged into the most frequent spelling
    assert t.data["Budget"][0] is None and t.data["Budget"][4] is None   # D-12 sentinel
    assert "Phone" in t.text_cols and audited(t, "D-21")


def test_dictionary_renames_columns():
    t = make({"fd_burn": ["1", "2"], "Spend": ["3", "4"]}, unique=False)
    t.cfg["_dictionary"] = {"fd_burn": "Free Delivery Burn", "spend": None}
    clean(t)
    assert "Free Delivery Burn" in t.cols and not rules(t, "D-16")
    assert any(a["action"] == "renamed" and a["old_value"] == "fd_burn" for a in t.audit)


def test_truncated_values():
    t = clean(make({"Description": ["Customer complaint 1", "Customer complaint 2", "Customer complaint 3", "short", "tiny", "mini", "min", "a1"]}))
    assert rules(t, "D-23")


def test_fix_switched_off_proposes_instead_of_changing():
    t = clean(make({"Name": ["  Ann ", "Bo", "Cy"]}, fixes={"D-09": False}))
    assert t.data["Name"][0] == "  Ann " and rules(t, "D-09")[0]["proposed_value"] == "Ann"


def test_annotations_become_review_and_log():
    t = make({"Total": [None, "5"], "Other": ["#REF!", "1"]})
    t.annotations = [(0, "Total", "formula_uncached", None), (1, "Total", "external_link", "5"),
                     (0, "Other", "broken_reference", "#REF!")]
    clean(t)
    assert len(rules(t, "D-05")) == 1 and len(rules(t, "D-06")) == 1 and rules(t, "D-06")[0]["client_report"]
    assert t.data["Other"][0] is None and audited(t, "D-07")


# ── end to end: outputs and the review loop ──────────────────────────────────
@pytest.fixture
def run(tmp_path):
    rows = ["Order Date,Spend,Status,Brand"]
    rows += [f"2026-01-{d:02d},{100 + d},Pending,Acme" for d in range(1, 11)]
    rows += ["2026-13-01,abc,Pending,Acme", "2026-01-12,  ,NA,acme"]
    src = tmp_path / "in.csv"
    src.write_text("\n".join(rows), encoding="utf8")
    return tmp_path / "run", run_hygiene(str(src), str(tmp_path / "run"))


def test_outputs_written(run):
    out, res = run
    base = out / OUT
    for name in ("remediation_audit.json", "needs_review.json", "Hygiene_Review.xlsx", "run.json"):
        assert (base / name).exists()
    df = pl.read_parquet(next(base.glob("tables/*/data.parquet")))
    assert df.height == 12 and df["Spend"].dtype == pl.Int64 and df["Order Date"].dtype == pl.Date
    assert res["totals"]["needs_review_open"] >= 2


def test_review_upload_applies_decisions_and_rechecks_only_changes(run):
    out, _ = run
    base = out / OUT
    wb = openpyxl.load_workbook(base / "Hygiene_Review.xlsx")
    ws = wb["Needs_Review"]
    hdr = [c.value for c in ws[1]]
    for row in ws.iter_rows(min_row=2):
        rule = row[hdr.index("Rule")].value
        if rule == "D-02":
            row[hdr.index("Decision")].value, row[hdr.index("New_Value")].value = "New Value", "2026-01-11"
        if rule == "D-04":
            row[hdr.index("Decision")].value, row[hdr.index("New_Value")].value = "New Value", "not a number"
    wb.save(out / "edited.xlsx")
    apply_upload(out, out / "edited.xlsx", load_config())
    df = pl.read_parquet(next(base.glob("tables/*/data.parquet")))
    assert df["Order Date"].to_list()[10] == date(2026, 1, 11)
    assert df["Spend"].to_list()[10] is None                          # the bad New Value failed re-check -> NULL again
    audit = json.loads((base / "remediation_audit.json").read_text("utf8"))
    assert sum(a["action"] == "analyst_edit" for a in audit) >= 2
    items = json.loads((base / "needs_review.json").read_text("utf8"))
    assert any(i["status"] == "open" and i["rule"] == "D-04" for i in items)   # re-check raised a new item
    assert len(list((base / "review_versions").glob("*.uploaded.xlsx"))) == 1


def test_review_upload_rejected_when_structure_changes(run):
    out, _ = run
    wb = openpyxl.load_workbook(out / OUT / "Hygiene_Review.xlsx")
    wb["Data_1"].delete_rows(3)
    wb.save(out / "bad.xlsx")
    with pytest.raises(ReviewError) as e:
        apply_upload(out, out / "bad.xlsx", load_config())
    assert any("Row_ID" in p for p in e.value.problems)


def test_phase_a_output_end_to_end(tmp_path):
    """Phase A on the 70-row sample, then Phase B on its output folder: selection, annotations, sheet name, outputs."""
    from conftest import run as run_structure
    from src.hygiene.io import load_tables
    sample = Path("samples/sample_media_spend_70rows.xlsx")
    if not sample.exists():
        pytest.skip("sample workbook not present")
    _, pa = run_structure(tmp_path, [sample])
    tables, bad, skipped = load_tables(pa, load_config())
    assert not bad and tables and all(t.sheet == "Media Spend" for t in tables)
    # Phase A sets the `Total` row aside as a footer; the `Total Clicks` formula (G4) stays annotated in the table
    assert ("Total Clicks", "formula_uncached") in {(c, k) for t in tables for _, c, k, _ in t.annotations}
    res = run_hygiene(str(pa), str(tmp_path / "run"))
    assert res["tables"] == [t.ref for t in tables] and not res["unreadable"]
    assert (tmp_path / "run" / "pipeline.log").exists()


def test_unedited_upload_changes_nothing(run):
    out, _ = run
    base = out / OUT
    before = (base / "remediation_audit.json").read_text("utf8")
    apply_upload(out, base / "Hygiene_Review.xlsx", load_config())
    assert (base / "remediation_audit.json").read_text("utf8") == before


def test_near_duplicate_rule_ignores_different_short_words():
    from src.hygiene.rules import _near_duplicate
    assert _near_duplicate("Truck Order", "Truck Order.", 0.9) and _near_duplicate("Facebook Ads", "Facebok Ads", 0.9)
    assert not _near_duplicate("first HCV Order", "first LCV Order", 0.9)


def test_unreadable_file_is_listed_and_dictionary_is_used(tmp_path):
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "good.csv").write_text("Spend,Mystery\n1,a\n2,b\n", encoding="utf8")
    (tmp_path / "in" / "broken.xlsx").write_bytes(b"not a zip")          # D-17: reported, not fatal
    (tmp_path / "dict.csv").write_text("column\nSpend\n", encoding="utf8")
    res = run_hygiene(str(tmp_path / "in"), str(tmp_path / "run"), "config/settings.yaml",
                      {"data_dictionary": str(tmp_path / "dict.csv")})
    assert [u["file"].endswith("broken.xlsx") for u in res["unreadable"]] == [True]
    items = json.loads((tmp_path / "run" / OUT / "needs_review.json").read_text("utf8"))
    assert [i["column"] for i in items if i["rule"] == "D-16"] == ["Mystery"]


def test_reject_restores_original_and_workbook_marks_cells(tmp_path):
    rows = ["Invoice_ID,Revenue"] + [f"I{i},{100 + i % 5}" for i in range(20)] + ["I99,50000"]
    (tmp_path / "in.csv").write_text(chr(10).join(rows), encoding="utf8")
    run_hygiene(str(tmp_path / "in.csv"), str(tmp_path / "run"))
    base = tmp_path / "run" / OUT
    wb = openpyxl.load_workbook(base / "Hygiene_Review.xlsx")
    assert wb.sheetnames[:4] == ["Summary", "Needs_Review", "Auto_Fix_Log", "Tables"]
    assert any(r[3] == "D-14" for r in wb["Summary"].iter_rows(values_only=True))
    cell = wb["Data_1"].cell(row=22, column=4)                        # the capped Revenue cell (after _row_id, _excel_row)
    assert cell.fill.fgColor.rgb.endswith("FFEB9C") and "D-14" in cell.comment.text
    ws = wb["Needs_Review"]
    hdr = [c.value for c in ws[1]]
    for row in ws.iter_rows(min_row=2):
        if row[hdr.index("Rule")].value == "D-14":
            row[hdr.index("Decision")].value = "Reject"
    wb.save(tmp_path / "edited.xlsx")
    apply_upload(tmp_path / "run", tmp_path / "edited.xlsx", load_config())
    df = pl.read_parquet(next(base.glob("tables/*/data.parquet")))
    assert df["Revenue"].to_list()[-1] == 50000                         # original value restored


def test_symbols_keep_values_apart_and_skewed_columns_are_not_capped():
    from src.hygiene.rules import _near_duplicate
    assert not _near_duplicate("5% off on all orders", "\u20b95 off on all orders", 0.9)
    assert _near_duplicate("2 wheeler order", "2-wheeler order", 0.9)
    skewed = [str(x) for x in [10] * 10 + [11] * 10 + [5000, 6000, 7000, 8000]]
    t = clean(make({"Clicks": skewed}))
    assert t.data["Clicks"][-4:] == ["5000", "6000", "7000", "8000"] and rules(t, "D-14")[0]["scope"] == "column"


def test_d15_flags_the_column_and_never_changes_the_data(tmp_path):
    rows = ["Invoice_ID,Loss,Spend"] + [f"I{i},{10 + i if i != 2 else -12},{100 + i}" for i in range(6)]
    (tmp_path / "in.csv").write_text(chr(10).join(rows), encoding="utf8")
    run_hygiene(str(tmp_path / "in.csv"), str(tmp_path / "run"))
    base = tmp_path / "run" / OUT
    df = pl.read_parquet(next(base.glob("tables/*/data.parquet")))
    assert df["Loss"].to_list() == [10, 11, -12, 13, 14, 15]            # the negative is still there
    wb = openpyxl.load_workbook(base / "Hygiene_Review.xlsx")
    ws = wb["Needs_Review"]
    hdr = [c.value for c in ws[1]]
    assert [r[hdr.index("Rule")].value for r in ws.iter_rows(min_row=2)].count("D-15") == 1                                                    # one item, for Loss; Spend has no negatives


# ── columns added after Phase B ──────────────────────────────────────────────
def _run_with_one_table(tmp_path, n=12):
    rows = ["Invoice_ID,Revenue"] + [f"I{i},{100 + i}" for i in range(n)]
    (tmp_path / "in.csv").write_text(chr(10).join(rows), encoding="utf8")
    run_hygiene(str(tmp_path / "in.csv"), str(tmp_path / "run"))
    return tmp_path / "run" / OUT


def _paste(base, tmp_path, columns: dict):
    """Open the review workbook, paste {header: [values]} to the right of Data_1 and save it as edited.xlsx."""
    wb = openpyxl.load_workbook(base / "Hygiene_Review.xlsx")
    ws = wb["Data_1"]
    start = ws.max_column + 1
    for k, (name, vals) in enumerate(columns.items()):
        ws.cell(row=1, column=start + k, value=name)
        for r, v in enumerate(vals, start=2):
            ws.cell(row=r, column=start + k, value=v)
    wb.save(tmp_path / "edited.xlsx")
    return tmp_path / "edited.xlsx"


def test_columns_pasted_after_phase_b_are_added_and_checked(tmp_path):
    base = _run_with_one_table(tmp_path)
    edited = _paste(base, tmp_path, {"Region": ["North", "NORTH"] + ["North"] * 10, "Status": ["ok", "n/a"] + ["ok"] * 10,
                                     "Clicks": ["1,200"] + ["100"] * 10 + ["900000"]})
    apply_upload(tmp_path / "run", edited, load_config())
    df = pl.read_parquet(next(base.glob("tables/*/data.parquet")))
    assert df.columns[-3:] == ["Region", "Status", "Clicks"] and df["Revenue"].to_list() == list(range(100, 112))   # old data untouched
    assert df["Region"].to_list()[:2] == ["North", "North"] and df["Status"].to_list()[:2] == ["ok", None]   # D-10 case, D-18 'n/a' -> NULL
    assert df["Clicks"].to_list()[0] == 1200                                           # E-01 thousands separator
    needs = json.loads((base / "needs_review.json").read_text("utf8"))
    assert any(i["column"] == "Clicks" and i["rule"] == "D-14" for i in needs)        # the 900000 spike is capped, reviewable
    audit = json.loads((base / "remediation_audit.json").read_text("utf8"))
    assert {a["column"] for a in audit if a["action"] == "column_added"} == {"Region", "Status", "Clicks"}
    again = openpyxl.load_workbook(base / "Hygiene_Review.xlsx")["Data_1"]
    assert [c.value for c in again[1]][-3:] == ["Region", "Status", "Clicks"]          # the next download has them too


def test_a_pasted_column_must_be_new_and_have_values(tmp_path):
    base = _run_with_one_table(tmp_path)
    for name, vals, words in (("revenue", ["1"] * 12, "repeats an existing column"),
                              ("Notes", [None] * 12, "has no values")):
        edited = _paste(base, tmp_path, {name: vals})
        with pytest.raises(ReviewError) as e:
            apply_upload(tmp_path / "run", edited, load_config())
        assert words in str(e.value)
    df = pl.read_parquet(next(base.glob("tables/*/data.parquet")))
    assert df.columns[-1] == "Revenue"                                                 # a rejected upload changes nothing


def test_d15_flags_any_numeric_column_that_has_negatives_and_nothing_else():
    t = clean(make({"Clicks": ["10", "-3", "12", "-40", "11", "10"], "Orders": ["5", "6", "7", "8", "9", "10"],
                    "Notes": ["a", "-b", "c", "d", "e", "f"], "Refunds": ["0", "-5", "0", "0", "0", "-2"]}))
    found = {r["column"]: r for r in rules(t, "D-15")}
    assert sorted(found) == ["Clicks", "Refunds"] and all(r["scope"] == "column" for r in found.values())
    assert "2 negative value(s)" in found["Clicks"]["why_flagged"] and "-3, -40" in found["Clicks"]["why_flagged"]
    assert t.data["Clicks"][1] == "-3" and t.data["Clicks"][3] == "-40"                      # nothing changed
    assert not [a for a in t.audit if a["rule"] == "D-15"]
