"""Phase D checks (spec DP-D §3) on one table: N-16 to N-20. Every check flags; none changes the data.

Everything runs on the configured Trend Line axis (§3.1): each variable is summed per Period date, a date that is not
in Period Mapping is left out (Unmapped_or_Missing says how many rows), and a configured date with no row has no value
(zero on the Trend Line). Period labels (MOM / QOQ / HOH / YOY) come from Period Mapping, so fiscal periods work; the
same period last year is the period holding the same dates a year earlier (`last_year`).

A rule without its threshold does not run (§3.6); Rules_Used lists it as INACTIVE. Each finding has a stable id (code +
what it is about, values included), so a Review mark holds on a re-run of the same data and is asked again when the
data behind the finding changes."""
from __future__ import annotations

import bisect
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date

from .config import PERIOD_TYPES, THRESHOLDS, ClientConfig
from .io import Layout, variable_columns
from src.common.dataset import Dataset
from src.common.values import Periods
from src.common.values import classify, finding_id, render

# code -> (name, what it checks). {key} is replaced by the configured threshold.
CHECKS = {
    "N-16": ("Abnormal Trend", "Robust Z-score (0.6745 x (value - median) / MAD) of each Support and Spend stream "
                               "against its own history; zero and blank values left out, at least {min_values} values. "
                               "Flag |z| >= {robust_z_threshold}, or any value off the median when MAD = 0."),
    "N-17": ("Period % Change", "Support and Spend totals per MOM / QOQ / HOH / YOY period against the sequential "
                                "period and the same period last year. Flag |change| >= the period type's threshold."),
    "N-18": ("Ratio % Change", "Each Ratio Definitions ratio (Numerator total / Denominator total) per period against "
                               "the sequential period and the same period last year. Flag |change| >= the ratio's "
                               "own threshold."),
    "N-19": ("Spend/Support Availability", "Dates where exactly one of a pair's Support and Spend has a non-zero "
                                           "value. Always on for a pair with both variables in the data."),
    "N-20": ("Spend Concentration", "Per Bucket + Aggregation 1, per period and overall: flag when the top "
                                    "{top_n} tactics hold more than {concentration_pct}% of the spend."),
}
MIN_VALUES = 5                         # N-16: a stream needs at least 5 non-zero values (spec §3.2)
CHANGES = {t: f"{t}_Changes" for t in PERIOD_TYPES}
NOT_ZERO = "NOT ASSESSED - ZERO TOTAL SPEND"
NOT_ENOUGH = "NOT ASSESSED - INSUFFICIENT TACTICS"


@dataclass
class Finding:
    code: str
    sheet: str                        # the report sheet its row is on
    reason: str                       # one line about this finding
    row: dict                         # its row on `sheet`
    key: tuple                        # what makes it the same finding on a re-run (hashed into `id`)
    variables: tuple = ()             # the variable(s) it is about
    periods: list = field(default_factory=list)    # periods / dates involved, as text
    cells: list = field(default_factory=list)      # (row_id, column) cells involved, marked on the Data sheet
    status: str = "open"              # open | confirmed | valid | corrected (from the analyst's Review)
    review: str | None = None         # the Review mark behind `status`
    listed: bool = True               # on its sheet (False beyond max_sheet_rows)
    note: str | None = None           # e.g. "marked Corrected in upload 2, still flagged"
    id: str = ""

    def __post_init__(self):
        self.id = self.id or finding_id(self.code, self.key)

    @property
    def check(self) -> str:
        return CHECKS[self.code][0]

    def as_dict(self, client_codes=()) -> dict:
        return {"finding_id": self.id, "code": self.code, "check": self.check, "sheet": self.sheet,
                "status": self.status, "review": self.review, "listed": self.listed, "variables": list(self.variables),
                "periods": self.periods, "row_ids": list(dict.fromkeys(r for r, _ in self.cells)),
                "reason": self.reason, "note": self.note,
                "detail": {k: render(v) if isinstance(v, date) else v for k, v in self.row.items()
                           if not k.startswith("_")},
                "client_report": self.code in client_codes}


@dataclass
class Result:
    findings: list[Finding]
    sheets: dict[str, list[dict]]     # report sheet -> rows; a row with a finding has it under "_finding"
    rules: list[dict]                 # Rules_Used rows
    inactive: list[str]               # rules (or ratios) not running for lack of a threshold or a variable
    pairs: dict[str, int]             # Variable Mapping Y pairs: mapped / run / with spend
    axis: dict                        # Trend Line axis and the rows left out of it


def evaluate(ds: Dataset, layout: Layout, cc: ClientConfig, cfg: dict) -> Result:
    return _Checker(ds, layout, cc, cfg).run()


# ── value helpers ────────────────────────────────────────────────────────────
def change(cur, prev):
    """% change, as the reference: None without a previous value or when it is 0."""
    if cur is None or prev is None or prev == 0:
        return None
    return (cur - prev) / abs(prev) * 100


def _div(n, d):
    return None if n is None or d is None or d == 0 else n / d


def _sum(vals):
    nums = [v for v in vals if v is not None]
    return sum(nums) if nums else None


def _k(x):
    """A value as part of a finding key: 12 significant digits, so summation order never changes an id."""
    return None if x is None else float(f"{x:.12g}")


def _fmt(x) -> str:
    return "-" if x is None else render(round(x, 4)) or "0"


def _pct(x) -> str:
    return f"{x:+.1f}%"


def _year_before(d: date) -> date:
    return d.replace(year=d.year - 1, day=28) if (d.month, d.day) == (2, 29) else d.replace(year=d.year - 1)


def last_year(dates: list[date], labels: dict[date, str], start: dict[str, date]) -> dict[str, str]:
    """Period label -> the label of the same period a year earlier, from Period Mapping alone: each configured date
    votes for the label of the configured date nearest to it a year before (within half the usual gap between dates,
    so weekly dates find the week a year back); the label with most votes wins. A period whose year-earlier dates are
    not configured gets none (spec §3.3: the comparison is then omitted)."""
    gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
    tol = statistics.median(gaps) / 2 if gaps else 0
    votes: dict[str, Counter] = defaultdict(Counter)
    for d in dates:
        cur = labels[d]
        if not cur:
            continue
        target = _year_before(d)
        j = bisect.bisect_left(dates, target)
        best = min((dates[k] for k in (j - 1, j) if 0 <= k < len(dates)), key=lambda x: (abs((x - target).days), x))
        prior = labels[best]
        if abs((best - target).days) <= tol and prior and prior != cur and start[prior] < start[cur]:
            votes[cur][prior] += 1
    return {cur: min(c, key=lambda m: (-c[m], start[m])) for cur, c in votes.items()}


def _stream(values) -> tuple[float | None, float | None]:
    """(median, MAD) of the non-zero values of a stream, or (None, None) below MIN_VALUES values."""
    vals = [v for v in values if v is not None and v != 0]
    if len(vals) < MIN_VALUES:
        return None, None
    med = statistics.median(vals)
    return med, statistics.median(abs(v - med) for v in vals)


def _score(x, med, mad, thr) -> tuple[float | None, bool, bool]:
    """(robust z, flagged, flagged by the MAD = 0 fallback). Zero and blank values are never scored."""
    if x is None or x == 0 or med is None:
        return None, False, False
    if mad == 0:
        return None, x != med, x != med
    z = 0.6745 * (x - med) / mad
    return z, abs(z) >= thr, False


class _Checker:
    def __init__(self, ds: Dataset, layout: Layout, cc: ClientConfig, cfg: dict):
        self.ds, self.lay, self.cc, self.cfg = ds, layout, cc, cfg
        self.th = cc.thresholds
        self.names = variable_columns(layout)
        parse = Periods(ds.data[layout.period], cfg["dayfirst"])
        axis = set(cc.axis.dates)
        self.rows_of: dict[date, list[int]] = defaultdict(list)
        self.outside: Counter = Counter()
        self.undated: list[int] = []
        for i, v in enumerate(ds.data[layout.period]):
            d = parse(v)
            if d is None:
                self.undated.append(i)
            elif d in axis:
                self.rows_of[d].append(i)
            else:
                self.outside[d] += 1
        self.observed = sorted(self.rows_of)
        self.rid = ds.row_ids
        self.labels = cc.axis.labels
        self.start = {t: {} for t in PERIOD_TYPES}            # label -> its first configured date
        for t in PERIOD_TYPES:
            for d in cc.axis.dates:
                if self.labels[t][d]:
                    self.start[t].setdefault(self.labels[t][d], d)
        self.periods = {t: sorted({self.labels[t][d] for d in self.observed if self.labels[t][d]},
                                  key=self.start[t].get) for t in PERIOD_TYPES}
        self.ly = {t: last_year(cc.axis.dates, self.labels[t], self.start[t]) for t in PERIOD_TYPES}
        self._daily: dict[str, dict] = {}
        self._totals: dict[tuple, dict] = {}
        self.unmapped: list[dict] = []
        self.run_pairs = []                                   # (Pair, support column, spend column or None)
        for p in cc.pairs:
            sup = self.names.get(p.support)
            spd = self.names.get(p.spend) if p.spend else None
            if sup is None:
                self.unmapped.append({"Support Variable": p.support, "Spend Variable": p.spend,
                                      "Reason": f"Support variable not found in input file: {p.support}"})
                continue
            if p.spend and spd is None:
                self.unmapped.append({"Support Variable": p.support, "Spend Variable": p.spend,
                                      "Reason": "Spend variable not found; Support changes were still calculated."})
            self.run_pairs.append((p, sup, spd))

    # ── streams ──────────────────────────────────────────────────────────────
    def daily(self, col: str) -> dict[date, float | None]:
        """Configured date -> sum of the column's numeric values on that date (None: no numeric value)."""
        if col not in self._daily:
            vals = self.ds.data[col]
            out = {}
            for d in self.observed:
                nums = [x for k, x in (classify(vals[i]) for i in self.rows_of[d]) if k == "number"]
                out[d] = sum(nums) if nums else None
            self._daily[col] = out
        return self._daily[col]

    def totals(self, col: str | None, t: str) -> dict[str, float | None]:
        """Period label -> the column's total over the period's dates (None: no numeric value in the period)."""
        if col is None:
            return {}
        if (col, t) not in self._totals:
            per: dict[str, list] = defaultdict(list)
            for d, v in self.daily(col).items():
                if self.labels[t][d]:
                    per[self.labels[t][d]].append(v)
            self._totals[(col, t)] = {lab: _sum(vs) for lab, vs in per.items()}
        return self._totals[(col, t)]

    def comparisons(self, t: str):
        """(current, sequential, same period last year or None) for every period after the first."""
        ps = self.periods[t]
        present = set(ps)
        for i in range(1, len(ps)):
            ly = self.ly[t].get(ps[i])
            yield ps[i], ps[i - 1], ly if ly in present else None

    def cells(self, d: date, cols) -> list[tuple[str, str]]:
        return [(self.rid[i], c) for i in self.rows_of.get(d, []) for c in cols if c]

    # ── run ──────────────────────────────────────────────────────────────────
    def run(self) -> Result:
        sheets: dict[str, list[dict]] = {"Trend Line": self.trend_line()}
        for t in PERIOD_TYPES:
            sheets[CHANGES[t]] = self.changes(t)
        ratios = self.active_ratios()
        sheets["Ratio_Changes"] = self.ratio_changes(ratios)
        detail, skew = self.concentration()
        sheets |= {"Spend Skewness": skew, "Spend_Support_Availability": self.availability(),
                   "Outlier_Details": self.abnormal(), "Spend_Concentration": detail}
        findings = [r["_finding"] for rows in sheets.values() for r in rows if "_finding" in r]
        ids = Counter()
        for f in findings:                                    # ids are unique by construction; this is a safety net
            ids[f.id] += 1
            if ids[f.id] > 1:
                f.id = f"{f.id}-{ids[f.id]}"
        limit = int(self.cfg["max_sheet_rows"])
        for rows in sheets.values():
            for r in rows[limit:]:
                if "_finding" in r:
                    r["_finding"].listed = False
        rules, inactive = self.rules_used(ratios)
        sheets["Unmapped_or_Missing"] = self.unmapped + self.axis_notes()
        pairs = {"mapped": len(self.cc.pairs), "run": len(self.run_pairs),
                 "with_spend": sum(spd is not None for _, _, spd in self.run_pairs)}
        axis = {"configured_dates": len(self.cc.axis.dates), "first": self.cc.axis.dates[0].isoformat(),
                "last": self.cc.axis.dates[-1].isoformat(), "dates_with_data": len(self.observed),
                "rows_outside_axis": sum(self.outside.values()), "rows_not_dated": len(self.undated),
                "periods": {t: len(self.periods[t]) for t in PERIOD_TYPES}}
        return Result(findings, sheets, rules, inactive, pairs, axis)

    def axis_notes(self) -> list[dict]:
        out = []
        if self.outside:
            dates = sorted(self.outside)
            out.append({"Support Variable": "", "Spend Variable": "", "Reason":
                        f"{sum(self.outside.values())} row(s) on {len(dates)} Period date(s) not in Period Mapping, "
                        f"left out of every check: {', '.join(d.isoformat() for d in dates[:10])}"
                        + (" ..." if len(dates) > 10 else "")})
        if self.undated:
            out.append({"Support Variable": "", "Spend Variable": "", "Reason":
                        f"{len(self.undated)} row(s) whose Period is not a date, left out of every check: "
                        f"{', '.join(self.rid[i] for i in self.undated[:10])}" + (" ..." if len(self.undated) > 10 else "")})
        return out

    # ── Trend Line ───────────────────────────────────────────────────────────
    def trend_line(self) -> list[dict]:
        """Every pair on every configured date; a date without data is zero (§1.1)."""
        rows = []
        for p, sup, spd in self.run_pairs:
            sv, pv = self.daily(sup), self.daily(spd) if spd else None
            for d in self.cc.axis.dates:
                rows.append({"Date": d, "Description": p.description, "Bucket": p.bucket,
                             "Performance Variable": p.support, "Spend Variable": p.spend if spd else "",
                             "Support": sv.get(d) or 0, "Spend": (pv.get(d) or 0) if pv is not None else None})
        return rows

    # ── N-16 ─────────────────────────────────────────────────────────────────
    def abnormal(self) -> list[dict]:
        thr = self.th["robust_z_threshold"]
        if thr is None:
            return []
        rows = []
        reported: set = set()                                 # a Spend stream shared by several pairs is flagged once
        for p, sup, spd in self.run_pairs:
            sv, pv = self.daily(sup), self.daily(spd) if spd else {}
            s_med, s_mad = _stream(sv.values())
            x_med, x_mad = _stream(pv.values()) if spd else (None, None)
            for d in self.observed:
                s, x = sv.get(d), pv.get(d)
                sz, sf, s0 = _score(s, s_med, s_mad, thr)
                xz, xf, x0 = _score(x, x_med, x_mad, thr) if spd else (None, False, False)
                x_new = xf and (spd, d) not in reported
                if xf:
                    reported.add((spd, d))
                if not (sf or x_new):
                    continue
                sides, why = [], []
                for flag, fallback, label, var, val, z, med, mad in (
                        (sf, s0, "Support", p.support, s, sz, s_med, s_mad),
                        (x_new, x0, "Spend", p.spend, x, xz, x_med, x_mad)):
                    if flag:
                        sides.append((label, _k(val)))
                        why.append(f"{label} {var} = {_fmt(val)}: " + (f"MAD = 0, differs from the median {_fmt(med)}"
                                   if fallback else f"robust z {z:.2f} (median {_fmt(med)}, MAD {_fmt(mad)})"))
                row = {"Date": d, "Description": p.description, "Bucket": p.bucket, "Support Variable": p.support,
                       "Spend Variable": p.spend, "Support Value": s, "Support Stream Median": s_med,
                       "Support MAD": s_mad, "Support Robust Z-score": sz, "Support Status": "OUTLIER" if sf else "PASS",
                       "Spend Value": x, "Spend Stream Median": x_med, "Spend MAD": x_mad, "Spend Robust Z-score": xz,
                       "Spend Status": "OUTLIER" if xf else ("PASS" if spd else "NOT MAPPED"),
                       "Outlier Reason": "; ".join(f"{label} outlier" + (" (MAD=0 fallback)" if fallback else "")
                                                   for label, flag, fallback in (("Support", sf, s0), ("Spend", xf, x0))
                                                   if flag)}
                row["_finding"] = Finding("N-16", "Outlier_Details", f"{d}: " + "; ".join(why), row,
                                          (p.support, p.spend, d.isoformat(), tuple(sides)),
                                          tuple(v for v in (p.support, p.spend) if v), [d.isoformat()],
                                          self.cells(d, [sup if sf else None, spd if x_new else None]))
                rows.append(row)
        return rows

    # ── N-17 ─────────────────────────────────────────────────────────────────
    def changes(self, t: str) -> list[dict]:
        thr = self.th[f"{t.lower()}_pct"]
        if thr is None:
            return []
        rows = []
        reported: set = set()                                 # a Spend total shared by several pairs is flagged once
        for p, sup, spd in self.run_pairs:
            sv, pv = self.totals(sup, t), self.totals(spd, t)
            for cur, prev, ly in self.comparisons(t):
                vals = {"S": (sv.get(cur), sv.get(prev), sv.get(ly) if ly else None),
                        "X": (pv.get(cur), pv.get(prev), pv.get(ly) if ly else None)}
                ch = {k: (change(c, q), change(c, y) if ly else None) for k, (c, q, y) in vals.items()}
                x_hit = any(v is not None and abs(v) >= thr for v in ch["X"]) and (spd, cur, prev, ly) not in reported
                if x_hit:
                    reported.add((spd, cur, prev, ly))
                hits = [v for k in "XS" for v in ch[k] if v is not None and abs(v) >= thr and (k == "S" or x_hit)]
                row = {"Period": cur, "Sequential Period": prev, "Last Year Same Period": ly,
                       "Description": p.description, "Bucket": p.bucket,
                       "Support Variable": p.support, "Current Support": vals["S"][0],
                       "Sequential Period Support": vals["S"][1], "Last Year Same Period Support": vals["S"][2],
                       "Sequential Period Support Change %": ch["S"][0],
                       "Last Year Same Period Support Change %": ch["S"][1],
                       "Spend Variable": p.spend, "Current Spend": vals["X"][0],
                       "Sequential Period Spend": vals["X"][1], "Last Year Same Period Spend": vals["X"][2],
                       "Sequential Period Spend Change %": ch["X"][0], "Last Year Same Period Spend Change %": ch["X"][1],
                       "Threshold %": thr, "Change Status": "ALERT" if hits else "PASS"}
                if hits:
                    why = [f"{label} {_pct(v)} vs {against}" for label, k in (("Support", "S"), ("Spend", "X"))
                           for v, against in zip(ch[k], (prev, ly)) if v is not None and abs(v) >= thr
                           and (k == "S" or x_hit)]
                    row["_finding"] = Finding(
                        "N-17", CHANGES[t], f"{t} {cur}: " + "; ".join(why) + f" (threshold {thr}%)", row,
                        (t, cur, prev, ly, p.support, p.spend, tuple(_k(v) for k in "SX" for v in vals[k])),
                        tuple(v for v in (p.support, p.spend) if v), [x for x in (cur, prev, ly) if x])
                rows.append(row)
        return rows

    # ── N-18 ─────────────────────────────────────────────────────────────────
    def active_ratios(self) -> list[tuple]:
        """(Ratio, numerator column, denominator column, why not run or None) per Ratio Definitions row."""
        out = []
        for r in self.cc.ratios:
            cols = (self.names.get(r.numerator), self.names.get(r.denominator))
            why = None
            for part, var, col in (("Numerator", r.numerator, cols[0]), ("Denominator", r.denominator, cols[1])):
                if why:
                    break
                if var not in self.cc.flagged:
                    why = f"{part} '{var}' is not on a Y row of Variable Mapping"
                elif col is None:
                    why = f"{part} '{var}' not found in input file"
                    self.unmapped.append({"Support Variable": "", "Spend Variable": "",
                                          "Reason": f"Ratio '{r.name}': {why}; the ratio is not checked."})
            if why is None and r.threshold is None:
                why = "no threshold configured"
            out.append((r, *cols, why))
        return out

    def ratio_changes(self, ratios: list[tuple]) -> list[dict]:
        rows = []
        for r, num, den, why in ratios:
            if why:
                continue
            for t in PERIOD_TYPES:
                nv, dv = self.totals(num, t), self.totals(den, t)
                for cur, prev, ly in self.comparisons(t):
                    rc, rp = _div(nv.get(cur), dv.get(cur)), _div(nv.get(prev), dv.get(prev))
                    ry = _div(nv.get(ly), dv.get(ly)) if ly else None
                    seq, lyc = change(rc, rp), change(rc, ry) if ly else None
                    hits = [(v, against) for v, against in ((seq, prev), (lyc, ly))
                            if v is not None and abs(v) >= r.threshold]
                    row = {"Period Type": t, "Period": cur, "Sequential Period": prev, "Last Year Same Period": ly,
                           "Ratio Name": r.name, "Numerator": r.numerator, "Denominator": r.denominator,
                           "Current Numerator": nv.get(cur), "Current Denominator": dv.get(cur), "Current Ratio": rc,
                           "Sequential Period Ratio": rp, "Sequential Period Ratio Change %": seq,
                           "Last Year Same Period Ratio": ry, "Last Year Same Period Ratio Change %": lyc,
                           "Threshold %": r.threshold, "Change Status": "ALERT" if hits else "PASS"}
                    if hits:
                        row["_finding"] = Finding(
                            "N-18", "Ratio_Changes",
                            f"{r.name} {t} {cur}: " + "; ".join(f"{_pct(v)} vs {a}" for v, a in hits)
                            + f" (threshold {r.threshold}%)", row,
                            (r.name, r.numerator, r.denominator, t, cur, prev, ly,
                             tuple(_k(v) for v in (nv.get(cur), dv.get(cur), nv.get(prev), dv.get(prev),
                                                   nv.get(ly) if ly else None, dv.get(ly) if ly else None))),
                            (r.numerator, r.denominator), [x for x in (cur, prev, ly) if x])
                    rows.append(row)
        return rows

    # ── N-19 ─────────────────────────────────────────────────────────────────
    def availability(self) -> list[dict]:
        rows = []
        for p, sup, spd in self.run_pairs:
            if spd is None:
                continue
            sv, pv = self.daily(sup), self.daily(spd)
            for d in self.observed:
                s, x = sv.get(d), pv.get(d)
                has_s, has_x = s not in (None, 0), x not in (None, 0)
                if has_s == has_x:
                    continue
                reason = "Spend available but Support unavailable" if has_x else "Support available but Spend unavailable"
                row = {"Date": d, "Description": p.description, "Bucket": p.bucket, "Support Variable": p.support,
                       "Spend Variable": p.spend, "Support Value": s, "Spend Value": x, "Reason": reason}
                row["_finding"] = Finding("N-19", "Spend_Support_Availability",
                                          f"{d}: {reason} ({p.support} = {_fmt(s)}, {p.spend} = {_fmt(x)})", row,
                                          (p.support, p.spend, d.isoformat(), has_s, _k(s if has_s else x)),
                                          (p.support, p.spend), [d.isoformat()], self.cells(d, [sup, spd]))
                rows.append(row)
        return rows

    # ── N-20 ─────────────────────────────────────────────────────────────────
    def concentration(self) -> tuple[list[dict], list[dict]]:
        """Spend_Concentration (every tactic of every group, ranked) and Spend Skewness (one row per ALERT group).
        Tactics are the Spend Variables of Y rows with an Aggregation 1, whether or not their Support was found (as
        the reference); periods are every Period Mapping label, so a period without spend is NOT ASSESSED."""
        top_n, limit = self.th["top_n"], self.th["concentration_pct"]
        if top_n is None or limit is None:
            return [], []
        tactics, seen = [], set()
        for p in self.cc.pairs:
            col = self.names.get(p.spend) if p.spend else None
            if p.aggregation and col and (p.bucket, p.aggregation, p.spend) not in seen:
                seen.add((p.bucket, p.aggregation, p.spend))
                tactics.append((p, col))
        groups: dict[tuple, list[dict]] = {}
        dates = self.cc.axis.dates
        for p, col in tactics:
            daily = self.daily(col)
            for t in PERIOD_TYPES:
                per: dict[str, list] = defaultdict(list)
                for d in dates:
                    if self.labels[t][d]:
                        per[self.labels[t][d]].append(daily.get(d))
                for lab in sorted(per, key=self.start[t].get):
                    groups.setdefault((t, lab, self.start[t][lab], p.bucket, p.aggregation), []).append(
                        (p, _sum(per[lab]) or 0.0))
            overall = _sum(daily.get(d) for d in dates)
            if overall is not None:
                groups.setdefault(("OVERALL", "Full Input Range", dates[0], p.bucket, p.aggregation), []).append(
                    (p, float(overall)))
        detail, skew = [], []
        for (t, lab, start, bucket, agg), members in groups.items():
            members = sorted(members, key=lambda m: -m[1])               # stable: ties keep Variable Mapping order
            total = sum(s for _, s in members)
            top = members[:top_n]
            top_spend = sum(s for _, s in top)
            pct = top_spend / total * 100 if total > 0 else None
            status = (NOT_ZERO if total <= 0 else NOT_ENOUGH if len(members) <= top_n
                      else "ALERT" if pct > limit else "PASS")
            names = "; ".join(p.description or p.spend for p, _ in top)
            spends = "; ".join(p.spend for p, _ in top)
            common = {"Period Type": t, "Period": lab, "Period Start": start, "Bucket": bucket, "Aggregation 1": agg}
            summary = {"Tactics in Aggregation 1": len(members), "Top Tactics Count": top_n,
                       "Total Aggregation 1 Spend": total, "Top Tactics Spend": top_spend}
            cum = 0.0
            for rank, (p, s) in enumerate(members, start=1):
                cum += s
                detail.append({**common, "Rank": rank, "Description": p.description, "Support Variable": p.support,
                               "Spend Variable": p.spend, "Tactic Spend": s,
                               "Spend Share %": s / total * 100 if total > 0 else 0,
                               "Cumulative Share %": cum / total * 100 if total > 0 else 0, **summary,
                               "Concentration %": pct, "Threshold %": limit, "Top Tactics": names,
                               "Top Spend Variables": spends, "Status": status})
            if status == "ALERT":
                reason = (f"Spends skewed towards few tactics: Top {top_n} tactics account for {pct:.1f}% of {agg} "
                          f"spend, above the {float(limit):.1f}% threshold.")
                row = {**common, **summary, "Skewness %": pct, "Threshold %": limit, "Top Tactics": names,
                       "Top Spend Variables": spends, "Issue Reason": reason, "Status": status}
                row["_finding"] = Finding("N-20", "Spend Skewness", f"{t} {lab}, {bucket} / {agg}: {reason}", row,
                                          (t, lab, bucket, agg, tuple((p.spend, _k(s)) for p, s in members)),
                                          tuple(p.spend for p, _ in top), [lab])
                skew.append(row)
        return detail, skew

    # ── Rules_Used (spec §3.6: every threshold configured, and every rule inactive for lack of one) ──────────
    def rules_used(self, ratios: list[tuple]) -> tuple[list[dict], list[str]]:
        rows, inactive = [], []

        def add(code, rule, key, value, status, note=""):
            rows.append({"Code": code, "Rule": rule, "Config Key": key, "Threshold": value, "Status": status,
                         "Note": note})
            line = f"{code} {CHECKS[code][0] if code == 'N-20' else rule}: {note}"
            if status == "INACTIVE" and line not in inactive:
                inactive.append(line)

        for key, (code, label, _) in THRESHOLDS.items():
            v = self.th[key]
            if code == "N-20":
                other = "concentration_pct" if key == "top_n" else "top_n"
                if v is None or self.th[other] is None:
                    missing = [k for k in ("top_n", "concentration_pct") if self.th[k] is None]
                    add(code, label, key, v, "INACTIVE",
                        f"no threshold configured for {' and '.join(missing)}: N-20 needs both and does not run")
                    continue
            if v is None:
                what = {"N-16": "N-16 does not run", "N-17": f"N-17 does not run for {key[:3].upper()}"}[code]
                add(code, label, key, None, "INACTIVE", f"no threshold configured: {what}")
            else:
                add(code, label, key, v, "ACTIVE")
        if not ratios:
            add("N-18", "Ratio % Change", "Ratio Definitions", None, "NOT CONFIGURED",
                "no Ratio Definitions rows: N-18 is opt-in per client")
        for r, _, _, why in ratios:
            add("N-18", f"Ratio: {r.name} ({r.numerator} / {r.denominator})", f"Ratio Definitions row {r.row}",
                r.threshold, "INACTIVE" if why else "ACTIVE", f"{why}: this ratio is not checked" if why else "")
        both = sum(spd is not None for _, _, spd in self.run_pairs)
        add("N-19", CHECKS["N-19"][0], "", None, "ALWAYS ON",
            f"no threshold needed; runs on {both} pair(s) with both Support and Spend in the data")
        for label, value in self.cc.unused:
            add("", label, "", value, "NOT USED", "no Phase D rule reads this row" +
                ("; ratio checks (CPM, CPC ...) are configured on the Ratio Definitions sheet"
                 if any(w in label.lower() for w in ("cpm", "cpc", "cpa", "cpv")) else ""))
        return rows, inactive
