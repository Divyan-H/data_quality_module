"""Phase C checks (spec DP-C §3) on one table: N-06 to N-15, plus the variable value checks of the reference report
(N-01 to N-05, Phase B's codes, repeated on the variable columns). Every check reports; none changes the data.

A table is a set of series: one per combination of its label columns (the columns before the Variables range, the
Period excepted - City x Channel, say). N-07, N-08, N-09, N-11 and N-13 look inside each series; N-12 and N-14 look at
the table's calendar. The step between periods is the table's own cadence (daily, weekly, monthly ...), read from its
dates. A table without label columns is one series, as the reference report assumes.

Each finding has a stable id (code + what it is about), so an analyst's "Valid - no change" survives re-runs, and the
row it shows on its sheet of Data_Validation_Report.xlsx."""
from __future__ import annotations

import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from statistics import median
from itertools import combinations

from .config import ClientConfig
from src.common.dataset import Dataset
from .layout import Layout
from src.common.values import Periods, month_label
from .values import SPECIAL_RE, text_of
from src.common.values import classify, clean_name, finding_id, number, render

# code -> (name in the report, what it checks). Wording from Data_Validation_Report's Check_Index.
CHECKS = {
    "N-01": ("Number / Text", "Checks that variable values are valid numeric values."),
    "N-02": ("Missing / Blank", "Checks for blank or missing cells."),
    "N-03": ("NULL / N/A", "Checks NULL, N/A, NA and None values."),
    "N-04": ("Spaces", "Checks leading/trailing spaces."),
    "N-05": ("Special Characters", "Checks unexpected characters in values."),
    "N-06": ("Execution Weeks", "Counts periods containing valid numeric values excluding absolute zero."),
    "N-07": ("No Executions", "Flags {weeks} or more consecutive periods of a series with no executions and records "
                              "each occurrence in the No_Executions sheet."),
    "N-08": ("WOW Duplicate", "Within each series: a non-zero value repeated over consecutive periods (Sequential, "
                              "one finding per run), or a fractional value recurring apart (Not Sequential)."),
    "N-09": ("MOM Duplicate", "Within each series: monthly totals of non-zero values; equal totals in consecutive "
                              "months (Sequential) or apart (Not Sequential), one finding per total."),
    "N-10": ("Duplicate Variable Data", "Checks variables having identical non-zero data."),
    "N-11": ("Duplicate Dates", "Checks a Period repeated within one series."),
    "N-12": ("Missing Dates", "Checks periods missing from the table's calendar."),
    "N-13": ("Date Order", "Checks chronological order of Period values within each series."),
    "N-14": ("Weekly Continuity", "Checks whether consecutive dates are one {step} apart."),
    "N-15": ("Duplicate Variable Names", "Checks duplicate variable names."),
}
# The sheet each code's findings are listed on.
SHEET = {**{c: "Variable_Checks" for c in ("N-01", "N-02", "N-03", "N-04", "N-05", "N-15")},
         "N-07": "No_Executions", "N-08": "WOW_Duplicates", "N-09": "MOM_Duplicates",
         "N-10": "Duplicate_Variable_Data", **{c: "Date_Integrity" for c in ("N-11", "N-12", "N-13", "N-14")}}
VALUE_CODES = ["N-02", "N-03", "N-04", "N-01", "N-05"]     # the reference's order within a variable
CADENCES = [("daily", 0, 2, 1, 0), ("weekly", 5, 9, 7, 0), ("monthly", 27, 32, 0, 1), ("quarterly", 85, 95, 0, 3),
            ("yearly", 355, 370, 0, 12)]                   # level, median gap range (days), step in days or months


def add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    y, m = d.year + m // 12, m % 12 + 1
    last = (date(y + m // 12, m % 12 + 1, 1) - timedelta(days=1)).day
    return date(y, m, min(d.day, last))


def cadence(distinct: list[date]):
    """(level, step) from the median gap between the table's distinct dates; step = the next period of a date, or
    None when the dates have no regular cadence (fewer than 3 dates, or a median gap of no known length)."""
    if len(distinct) < 3:
        return "unknown", None
    gap = median((b - a).days for a, b in zip(distinct, distinct[1:]))
    for level, lo, hi, days, months in CADENCES:
        if lo < gap <= hi:
            return level, ((lambda d, k=days: d + timedelta(days=k)) if days else (lambda d, k=months: add_months(d, k)))
    return "irregular", None


@dataclass
class Finding:
    code: str
    reason: str                       # one line about this finding
    row: dict                         # its row on SHEET[code]
    key: tuple                        # what makes it the same finding on a re-run (hashed into `id`)
    variables: tuple = ()             # the variable(s) it is about
    periods: list = field(default_factory=list)    # periods involved, as text (Errors detail columns)
    cells: list = field(default_factory=list)      # (row_id, column) cells involved, marked in the Data sheet
    status: str = "open"              # open | valid (marked "Valid - no change" by the analyst)
    id: str = ""

    def __post_init__(self):
        self.id = self.id or finding_id(self.code, self.key)

    @property
    def check(self) -> str:
        return CHECKS[self.code][0]

    @property
    def sheet(self) -> str:
        return SHEET[self.code]

    def as_dict(self, client_codes=()) -> dict:
        return {"finding_id": self.id, "code": self.code, "check": self.check, "sheet": self.sheet,
                "status": self.status, "variables": list(self.variables), "periods": self.periods,
                "row_ids": list(dict.fromkeys(r for r, _ in self.cells)), "reason": self.reason,
                "detail": {k: render(v) if isinstance(v, date) else v for k, v in self.row.items()},
                "client_report": self.code in client_codes}


@dataclass
class Result:
    findings: list[Finding]
    variables: list[dict]             # Execution_Weeks rows: every variable of the range, then mapped ones not found
    checked: list[str]                # variables run through the checks, in column order
    execution: dict[str, int]         # N-06 per variable
    meta: dict[str, tuple[str, str]]  # variable -> (bucket, description)
    dates: list[date | None]          # parsed Period of each row
    min_weeks: int
    info: list[tuple[str, str]]       # extra INFO rows for Overall_Summary
    level: str = "weekly"             # the table's cadence: daily, weekly, monthly ... (irregular / unknown: none)


def evaluate(ds: Dataset, layout: Layout, cc: ClientConfig, cfg: dict) -> Result:
    return _Checker(ds, layout, cc, cfg).run()


def _listing(values: list) -> str:
    return ", ".join(repr(v) for v in values[:10])


class _Checker:
    def __init__(self, ds: Dataset, layout: Layout, cc: ClientConfig, cfg: dict):
        self.ds, self.lay, self.cc, self.cfg = ds, layout, cc, cfg
        self.min_weeks = cc.min_weeks or int(cfg["min_zero_weeks"])
        self.skip = set(cfg.get("skip_codes", []))              # checks that do not apply to this table (see info)
        raw = ds.data[layout.period]
        parse = Periods(raw, cfg["dayfirst"])
        self.dates = [parse(v) for v in raw]
        self.ptext = [d.isoformat() if d else (render(v) or "") for d, v in zip(self.dates, raw)]
        self.rid = ds.row_ids
        self.level, self.step = cadence(sorted({d for d in self.dates if d}))
        labels = [ds.data[c] for c in layout.series]
        self.key = [" / ".join(render(col[i]) or "" for col in labels) for i in range(len(self.rid))] if labels \
            else [""] * len(self.rid)
        self.nulls = {str(t).strip().upper() for t in cfg["null_tokens"]}
        ignore = {str(b).strip().upper() for b in cfg["ignore_buckets"]}
        self.meta, self.status = {}, {}
        for v in layout.variables:
            m = cc.mapping.get(clean_name(v))
            self.meta[v] = (m["bucket"], m["description"]) if m else ("", "")
            self.status[v] = ("Ignored: repeats an earlier variable name (N-15)" if v in layout.repeated else
                              "Ignored: not in Variable Mapping" if m is None else
                              f"Ignored: Bucket '{m['bucket']}'" if m["bucket"].upper() in ignore else "Checked")
        self.checked = [v for v in layout.variables if self.status[v] == "Checked"]
        self.kinds = {v: [classify(x) for x in ds.data[v]] for v in layout.variables}
        self._orders: dict[tuple, list[int]] = {}

    # ── helpers ──────────────────────────────────────────────────────────────
    def series(self, code: str) -> list[list[int]]:
        """Each series' rows with a Period date inside `code`'s configured range, by date (ties keep row order)."""
        lo, hi = self.cc.ranges.get(code, (None, None))
        if (lo, hi) not in self._orders:
            groups: dict[str, list[int]] = defaultdict(list)
            for i, d in enumerate(self.dates):
                if d and (lo is None or d >= lo) and (hi is None or d <= hi):
                    groups[self.key[i]].append(i)
            self._orders[(lo, hi)] = [sorted(rows, key=lambda i: self.dates[i]) for rows in groups.values()]
        return self._orders[(lo, hi)]

    def next_to(self, a: date, b: date) -> bool:
        """Is `b` the period right after `a`?"""
        return self.step is not None and self.step(a) == b

    def month_label(self, i: int) -> str:
        """Period Mapping's label for row i's date (by text, then by date), else e.g. "January '24"."""
        text = (render(self.ds.data[self.lay.period][i]) or "").strip()
        return (self.cc.month_by_text.get(text) or self.cc.month_by_date.get(self.dates[i])
                or month_label(self.dates[i]))

    def var_row(self, v: str, i: int) -> dict:
        b, d = self.meta.get(v, ("", ""))
        return {"Series": self.key[i], "Variables": v, "Description": d, "Bucket": b}

    # ── run ──────────────────────────────────────────────────────────────────
    def run(self) -> Result:
        findings: list[Finding] = []
        for v in self.checked:
            findings += self.value_checks(v) + self.no_executions(v)
            findings += [] if "N-08" in self.skip else self.wow(v)
            findings += [] if "N-09" in self.skip else self.mom(v)
        findings += self.duplicate_data() + self.date_checks() + self.duplicate_names()
        ids = Counter()
        for f in findings:                                    # ids are unique by construction; this is a safety net
            ids[f.id] += 1
            if ids[f.id] > 1:
                f.id = f"{f.id}-{ids[f.id]}"
        execution = {v: sum(k == "number" and x != 0 for k, x in self.kinds[v]) for v in self.lay.variables}
        return Result(findings, self.variable_rows(execution), self.checked, execution, self.meta, self.dates,
                      self.min_weeks, self.info(), self.level)

    def variable_rows(self, execution: dict) -> list[dict]:
        """Execution_Weeks: N-06 for every variable of the range, then mapped variables the table does not have."""
        rows = [{"Bucket": self.meta[v][0], "Description": self.meta[v][1], "Variables": v, "Status": self.status[v],
                 "Execution Weeks": execution[v]} for v in self.lay.variables]
        present = {clean_name(v) for v in self.ds.columns}
        ignore = {str(b).strip().upper() for b in self.cfg["ignore_buckets"]}
        rows += [{"Bucket": m["bucket"], "Description": m["description"], "Variables": name,
                  "Status": "In Variable Mapping, not in the data", "Execution Weeks": None}
                 for name, m in self.cc.mapping.items() if name not in present and m["bucket"].upper() not in ignore]
        return rows

    def info(self) -> list[tuple[str, str]]:
        out = [("Series", f"{len(set(self.key))} series by {' x '.join(self.lay.series)}" if self.lay.series
                else "one series (no label columns before the Variables)"),
               ("Cadence", self.level if self.step else f"{self.level}: the missing-date and continuity checks "
                                                        "need a regular cadence and were not run")]
        bad = [self.rid[i] for i, d in enumerate(self.dates) if d is None]
        if bad:
            out.append(("Periods Not Read As Dates", f"{len(bad)} row(s), left out of the date checks: "
                                                     f"{', '.join(bad[:10])}" + (" ..." if len(bad) > 10 else "")))
        if self.cfg.get("derived") == "monthly":
            out.append(("Weekly From Monthly", "the weeks are calculated from monthly figures (see weekly_rollup.json); "
                        "the repeated-value checks N-08 and N-09 were not run, as the figures repeat by construction"))
        out.append(("No Executions Minimum Weeks", str(self.min_weeks)))
        for code, (lo, hi) in sorted(self.cc.ranges.items()):
            out.append((f"{CHECKS[code][0]} Date Range", f"{lo or 'start'} to {hi or 'end'}"))
        return out

    # ── N-01 to N-05 ─────────────────────────────────────────────────────────
    def value_checks(self, v: str) -> list[Finding]:
        vals, kinds = self.ds.data[v], self.kinds[v]
        texts = [text_of(x) for x in vals]
        filled = [k != "blank" for k, _ in kinds]
        masks = {
            "N-02": [not f for f in filled],
            "N-03": [f and t.strip().upper() in self.nulls for f, t in zip(filled, texts)],
            "N-04": [isinstance(x, str) and x != x.strip() for x in vals],
            "N-01": [k == "text" for k, _ in kinds],
            "N-05": [f and not (k == "number" and not isinstance(x, str)) and bool(SPECIAL_RE.search(t.strip()))
                     for f, (k, _), x, t in zip(filled, kinds, vals, texts)],
        }
        out = []
        for code in VALUE_CODES:
            idx = [i for i, hit in enumerate(masks[code]) if hit]
            if not idx:
                continue
            n, periods = len(idx), [self.ptext[i] for i in idx]
            distinct = list(dict.fromkeys(texts[i] for i in idx))
            reason = {"N-02": f"{n} blank/missing value(s). Periods: {', '.join(periods[:10])}",
                      "N-03": f"{n} NULL/N/A value(s) found.",
                      "N-04": f"{n} value(s) contain leading/trailing spaces: {_listing(distinct)}",
                      "N-01": f"{n} invalid numeric value(s): {_listing(distinct)}",
                      "N-05": f"{n} value(s) contain special characters: {_listing(distinct)}"}[code]
            b, d = self.meta[v]
            row = {"Code": code, "Check": CHECKS[code][0], "Bucket": b, "Description": d, "Variables": v,
                   "Count": n, "Periods": " | ".join(periods), "Detail": reason}
            out.append(Finding(code, reason, row, (v, tuple(sorted(self.rid[i] for i in idx))), (v,), periods,
                               [(self.rid[i], v) for i in idx]))
        return out

    # ── N-07 ─────────────────────────────────────────────────────────────────
    def no_executions(self, v: str) -> list[Finding]:
        """Per series: runs of >= min_weeks numeric zeros, each exactly one period after the last; a gap ends the run
        (kept if long enough) and never joins it to the next one."""
        zero = [k == "number" and x == 0 for k, x in self.kinds[v]]
        out = []
        for rows in self.series("N-07"):
            runs, cur = [], []
            for i in rows:
                if not zero[i]:
                    runs += [cur] if len(cur) >= self.min_weeks else []
                    cur = []
                elif cur and self.next_to(self.dates[cur[-1]], self.dates[i]):
                    cur.append(i)
                else:
                    runs += [cur] if len(cur) >= self.min_weeks else []
                    cur = [i]
            runs += [cur] if len(cur) >= self.min_weeks else []
            for n, run in enumerate(runs, start=1):
                start, end, s = self.dates[run[0]], self.dates[run[-1]], self.key[run[0]]
                row = {"Series": s, "Bucket": self.meta[v][0], "Description": self.meta[v][1], "Variables": v,
                       "Occurrence": n, "Start Period": start, "End Period": end, "Number of Weeks": len(run),
                       "All Periods": " | ".join(self.dates[i].isoformat() for i in run)}
                out.append(Finding("N-07", f"{(s + ': ') if s else ''}{len(run)} periods ({start} - {end}) with no "
                                   "executions", row, (v, s, start.isoformat(), end.isoformat()), (v,),
                                   [start.isoformat(), end.isoformat()], [(self.rid[i], v) for i in run]))
        return out

    # ── N-08 ─────────────────────────────────────────────────────────────────
    def repeats(self, code: str, v: str, points: list[tuple[date, float, list[int]]], whole_apart: bool,
                min_run: dict[bool, int], label, step) -> list[Finding]:
        """N-08 / N-09 on one series: `points` = (period, non-zero value, rows) by period. A value held over
        consecutive periods is one Sequential finding per run (at least `min_run[whole]` periods); a value recurring
        apart is one Not Sequential finding listing every period it holds (whole numbers only if `whole_apart`).
        Findings stay at most one per run or value, so a long or constant series cannot multiply them."""
        runs: list[list] = []
        for p in points:
            last = runs[-1][-1] if runs else None
            if last and last[1] == p[1] and step is not None and step(last[0]) == p[0]:
                runs[-1].append(p)
            else:
                runs.append([p])
        out, by_value = [], defaultdict(list)
        for run in runs:
            value = run[0][1]
            whole = float(value).is_integer()
            if len(run) >= min_run[whole]:
                out.append(self._repeat(code, v, run, "Sequential", label))
            if not whole or whole_apart:
                by_value[value].append(run)
        for value, rs in by_value.items():
            if len(rs) > 1:
                out.append(self._repeat(code, v, [p for r in rs for p in r], "Not Sequential", label))
        return out

    def _repeat(self, code: str, v: str, pts: list, kind: str, label) -> Finding:
        first, last, value = pts[0], pts[-1], pts[0][1]
        s = self.key[first[2][0]]
        when = [label(p[0]) for p in pts]
        row = {**self.var_row(v, first[2][0]), "Value": number(value), "Comparison Type": kind,
               "Occurrences": len(pts), "Periods": " | ".join(when)}
        if code == "N-08":
            row |= {"Period": last[0], "Compared Period": first[0]}
        else:
            row |= {"Month": label(last[0]), "Compared Month": label(first[0])}
        reason = (f"{(s + ': ') if s else ''}{number(value)} " +
                  (f"in {len(pts)} consecutive periods {when[0]} - {when[-1]}" if kind == "Sequential"
                   else f"recurs in {len(pts)} periods: {', '.join(when[:6])}{' ...' if len(when) > 6 else ''}"))
        return Finding(code, reason, row, (v, s, kind, repr(value), first[0].isoformat()), (v,), when,
                       [(self.rid[i], v) for p in pts for i in p[2]] if code == "N-08" else [])

    def wow(self, v: str) -> list[Finding]:
        min_run = {False: int(self.cfg["min_repeat_run"]), True: int(self.cfg["min_repeat_run_whole"])}
        out = []
        for rows in self.series("N-08"):
            pts = [(self.dates[i], x, [i]) for i in rows for k, x in [self.kinds[v][i]] if k == "number" and x != 0]
            out += self.repeats("N-08", v, pts, False, min_run, date.isoformat, self.step)
        return out

    # ── N-09 ─────────────────────────────────────────────────────────────────
    def mom(self, v: str) -> list[Finding]:
        """Per series: monthly totals of non-zero values, summed in row order as the reference does (rounded to 10
        significant digits, so float noise does not hide an equal total); consecutive equal months and equal totals
        apart, at most one finding per run or total."""
        out = []
        for rows in self.series("N-09"):
            totals: dict[date, float] = {}
            labels: dict[date, str] = {}
            for i in sorted(rows):
                k, x = self.kinds[v][i]
                if k == "number" and x != 0:
                    m = self.dates[i].replace(day=1)
                    totals[m] = totals.get(m, 0.0) + x
                    labels.setdefault(m, self.month_label(i))
            pts = [(m, float(f"{t:.10g}"), [rows[0]]) for m, t in sorted(totals.items()) if t != 0]
            out += self.repeats("N-09", v, pts, True, {False: 2, True: 2}, labels.get, lambda d: add_months(d, 1))
        return out

    # ── N-10 ─────────────────────────────────────────────────────────────────
    def duplicate_data(self) -> list[Finding]:
        """Variables whose non-zero values sit in the same rows with the same values. Grouping by that series finds
        every identical pair in one pass (the reference compares each pair of variables)."""
        series: dict[str, list[str]] = defaultdict(list)
        for v in self.checked:                                # a sum of per-cell hashes: row order does not matter
            acc, n = 0, 0
            for rid, (k, x) in zip(self.rid, self.kinds[v]):
                if k == "number" and x != 0:
                    acc += int.from_bytes(hashlib.blake2b(f"{rid}\x1f{x!r}".encode(), digest_size=16).digest(), "big")
                    n += 1
            if n:
                series[f"{acc % (1 << 128):032x}{n:x}"].append(v)
        pos = {v: n for n, v in enumerate(self.checked)}
        pairs = sorted(((a, b, key) for key, vs in series.items() for a, b in combinations(vs, 2)),
                       key=lambda p: (pos[p[0]], pos[p[1]]))
        out = []
        for a, b, key in pairs:
            digest = key[:12]
            row = {"Variable 1": a, "Variable 1 Description": self.meta[a][1], "Variable 1 Bucket": self.meta[a][0],
                   "Variable 2": b, "Variable 2 Description": self.meta[b][1], "Variable 2 Bucket": self.meta[b][0],
                   "Reason": "Identical non-zero variable data"}
            out.append(Finding("N-10", f"'{a}' and '{b}' have identical non-zero data", row, (a, b, digest), (a, b)))
        return out

    # ── N-11 to N-14 ─────────────────────────────────────────────────────────
    def _date_finding(self, code: str, period: date, related: date | None, detail: str, key: tuple,
                      rows: list[int] = ()) -> Finding:
        row = {"Code": code, "Check": CHECKS[code][0], "Series": self.key[rows[0]] if rows else "", "Period": period,
               "Related Period": related, "Detail": detail, "Row_IDs": " | ".join(self.rid[i] for i in rows)}
        return Finding(code, detail, row, key, (), [period.isoformat()],
                       [(self.rid[i], self.lay.period) for i in rows])

    def date_checks(self) -> list[Finding]:
        """N-11 and N-13 within each series (in row order), N-12 and N-14 on the table's calendar of distinct dates."""
        rows_of: dict[tuple[str, date], list[int]] = defaultdict(list)
        last: dict[str, tuple[int, date]] = {}
        out = []
        for i, d in enumerate(self.dates):
            if d is None:
                continue
            s = self.key[i]
            rows_of[(s, d)].append(i)
            if s in last and d < last[s][1]:                  # N-13: dated before the series' row above it
                out.append(self._date_finding("N-13", d, last[s][1], f"{(s + ': ') if s else ''}{d} comes after "
                                              f"{last[s][1]}", (self.rid[i], d.isoformat(), last[s][1].isoformat()), [i]))
            last[s] = (i, d)
        out = [self._date_finding("N-11", d, None, f"{(s + ': ') if s else ''}{d} appears {len(rows)} times",
                                  (d.isoformat(), tuple(sorted(self.rid[i] for i in rows))), rows)
               for (s, d), rows in sorted(rows_of.items(), key=lambda x: (x[0][1], x[0][0])) if len(rows) > 1] + out
        if self.step is None:
            return out
        distinct = sorted({d for _, d in rows_of})
        for a, b in zip(distinct, distinct[1:]):
            m = self.step(a)
            while m < b:                                      # N-12: one finding per missing period of a gap
                out.append(self._date_finding("N-12", m, a, f"no row for {m}: the dates jump from {a} to {b}",
                                              (m.isoformat(),)))
                m = self.step(m)
            if self.step(a) != b:                             # N-14: consecutive distinct dates not one period apart
                out.append(self._date_finding("N-14", b, a, f"{(b - a).days} days after {a} (expected "
                                              f"{(self.step(a) - a).days})", (a.isoformat(), b.isoformat())))
        return out

    # ── N-15 ─────────────────────────────────────────────────────────────────
    def duplicate_names(self) -> list[Finding]:
        """Header text repeated within the Variables range (case and spacing ignored)."""
        groups: dict[str, list[str]] = defaultdict(list)
        for v in self.lay.variables:
            groups[clean_name(self.lay.raw[v]).casefold()].append(v)
        out = []
        for key, cols in groups.items():
            if len(cols) < 2:
                continue
            name = clean_name(self.lay.raw[cols[0]])
            detail = (f"'{name}' appears {len(cols)} times in the header "
                      f"(columns {', '.join(self.lay.letters[c] for c in cols)})")
            row = {"Code": "N-15", "Check": CHECKS["N-15"][0], "Bucket": self.meta[cols[0]][0],
                   "Description": self.meta[cols[0]][1], "Variables": name, "Count": len(cols), "Periods": "",
                   "Detail": detail}
            out.append(Finding("N-15", detail, row, (key, len(cols)), tuple(cols)))
        return out
