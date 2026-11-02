"""One workbook with a mix of sheets, taken through the whole web flow the way an analyst does it: choose which sheets to
process, clean with the KPI rules, check the sheets the client config describes (one report per sheet), and download.

Sheets: Weekly (the planted Phase D sheet), Weekly copy (to drop), Notes (prose), Monthly, Daily.
"""
import io
import json
import random
import zipfile
from datetime import date, timedelta

import openpyxl
import polars as pl
import pytest

from conftest import Flow


def mixed_book(weekly_xlsx) -> bytes:
    wb = openpyxl.load_workbook(weekly_xlsx)
    wb.copy_worksheet(wb["Weekly"]).title = "Weekly copy"
    notes = wb.create_sheet("Notes")
    for line in ("Read me first", "These figures were supplied by the client.", "Do not forward.", "Contact: ops team"):
        notes.append([line])
    monthly = wb.create_sheet("Monthly")
    monthly.append(["Period", "Geography", "TV_Spend", "TV_Impressions"])
    for m in range(36):
        monthly.append([date(2023 + m // 12, m % 12 + 1, 1), "National", 3000 + 40 * m, 90000 + 700 * m])
    daily = wb.create_sheet("Daily")
    daily.append(["Date", "City", "Sales", "Rain", "Search", "Social", "Display", "All media"])
    rnd = random.Random(4)
    for k in range(60):
        a, b, c = (rnd.randint(20, 90) for _ in range(3))
        daily.append([date(2026, 3, 2) + timedelta(days=k), "Pune", 0 if k == 10 else 100 + 2 * k, 0 if k in (10, 20, 30) else 5 + k % 4,
                      a, b, c, a + b + c])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


@pytest.fixture
def mixed(flow, weekly_files):
    rid = flow.create([("mixed.xlsx", mixed_book(weekly_files["data"]))], name="mixed")
    flow.run_phase(rid, "A", {"llm": "off"})
    return rid


def by_sheet(tables):
    return {t["sheet"]: t for t in tables}


def test_phase_a_finds_every_sheet_and_the_analyst_chooses_which_go_on(flow, mixed):
    s = flow.summary(mixed, "A")
    found = by_sheet(s["tables"])
    assert {"Weekly", "Monthly", "Daily"} <= set(found) and any(n.startswith("Weekly copy") for n in found), sorted(found)
    assert "Notes" not in found                                                       # prose is not a table
    keep = [t["ref"] for sheet, t in found.items() if sheet in ("Weekly", "Monthly", "Daily")]
    r = flow.c.post(f"/api/v1/runs/{mixed}/phases/A/selection", json={"tables": keep})
    assert r.status_code == 200, r.text
    chosen = by_sheet(flow.summary(mixed, "A")["tables"])
    assert [sheet for sheet, t in chosen.items() if t["selected"]] == ["Weekly", "Monthly", "Daily"] or \
        {sheet for sheet, t in chosen.items() if t["selected"]} == {"Weekly", "Monthly", "Daily"}
    flow.accept(mixed, "A")
    flow.run_phase(mixed, "B")
    cleaned = flow.summary(mixed, "B")["tables"]
    assert {t["sheet"] for t in cleaned} == {"Weekly", "Monthly", "Daily"}          # the dropped sheets never reach Phase B


def test_the_whole_flow_on_a_mixed_workbook(flow, mixed, weekly_files):
    rid = mixed
    found = by_sheet(flow.summary(rid, "A")["tables"])
    keep = [t["ref"] for sheet, t in found.items() if sheet in ("Weekly", "Monthly", "Daily")]
    assert flow.c.post(f"/api/v1/runs/{rid}/phases/A/selection", json={"tables": keep}).status_code == 200
    flow.accept(rid, "A")

    # Phase B start screen: which columns can legitimately be zero?
    cols = by_sheet(flow.c.get(f"/api/v1/runs/{rid}/phases/B/columns").json()["tables"])
    assert cols["Daily"]["dated"] and {"Sales", "Rain"} <= {c["name"] for c in cols["Daily"]["columns"]}
    daily_ref = cols["Daily"]["ref"]
    flow.run_phase(rid, "B", {"fixes": {"D-27": True}, "zero_expected": {daily_ref: ["Rain"]}})
    b = flow.summary(rid, "B")
    assert {t["sheet"] for t in b["tables"]} == {"Weekly", "Monthly", "Daily"}
    daily = pl.read_csv(io.BytesIO(flow.download(rid, flow.data_id(rid, "csv", "Daily"))))
    assert daily["Sales"][10] == 120 and daily["Rain"][10] == 0 and daily["Rain"][20] == 0     # filled / kept: zero is valid
    assert [bk["sum_these"] for bk in b["buckets"] if bk["sheet"] == "Daily"] == [["Display", "Search", "Social"]]
    flow.accept(rid, "B")

    # Phase C: one config, every sheet it describes is checked, each with its own report; the others pass through
    cands = by_sheet(flow.c.get(f"/api/v1/runs/{rid}/phases/C/tables").json()["tables"])
    assert cands["Weekly"]["weekly"] and cands["Monthly"]["level"] == "monthly" and cands["Daily"]["level"] == "daily"
    cfg = weekly_files["config"].read_bytes()
    pv = next(p for p in flow.config(rid, "C", cfg).json()["phases"] if p["key"] == "C")["config"]["preview"]
    checked = {t["sheet"]: t["checked"] for t in pv["tables"]}
    assert checked["Weekly"] is True and checked["Monthly"] is True and checked["Daily"] is False   # Daily has none of the config's variables
    flow.run_phase(rid, "C")
    c = flow.c.get(f"/api/v1/runs/{rid}/phases/C/summary").json()
    assert [t["sheet"] for t in c["tables"]] == ["Weekly", "Monthly"] and {n["sheet"] for n in c["not_checked"]} == {"Daily"}     # one report per sheet
    assert len([f for f in flow.c.get(f"/api/v1/runs/{rid}/results").json()["files"] if f["id"].startswith("timeseries-report-")]) == 2
    flow.accept(rid, "C")

    # Phase D
    flow.config(rid, "D", cfg)
    flow.run_phase(rid, "D")
    d = flow.c.get(f"/api/v1/runs/{rid}/phases/D/summary").json()
    assert [t["sheet"] for t in d["tables"]] == ["Weekly", "Monthly"]
    assert [w["sheet"] for w in d["weekly"]] == ["Monthly"]                      # only the monthly sheet is also spread over weeks
    flow.accept(rid, "D")

    # Results: one data file set per kept sheet plus the weekly one; nothing from the dropped sheets
    res = flow.c.get(f"/api/v1/runs/{rid}/results").json()
    names = {f["id"] for f in res["files"] if f["id"].startswith("data-") and f["id"].endswith("-csv")}
    assert names == {"data-weekly-csv", "data-monthly-csv", "data-daily-csv", "data-monthly_weekly-csv"}, names
    weekly = pl.read_csv(io.BytesIO(flow.download(rid, "data-monthly_weekly-csv")))
    monthly = pl.read_csv(io.BytesIO(flow.download(rid, "data-monthly-csv")))
    assert weekly["TV_Spend"].sum() == pytest.approx(monthly["TV_Spend"].sum())
    z = zipfile.ZipFile(io.BytesIO(flow.c.get(f"/api/v1/runs/{rid}/bundle").content))
    assert "MANIFEST.json" in z.namelist() and z.testzip() is None
    assert res["complete"]
