"""Plain-English commands for the reshaping operations (and the two shape-safe ones Phase B also takes: split a
column into columns, and if / else). Mixed into CommandParser; each handler returns a Plan, raises Ask, or returns
None when the words are not its command.

    unpivot Day 1 to Day 31 into rows as Day and Reactivated        turn the Day columns into rows
    pivot Inactivity Bucket into columns using Reactivated [by Date, City] [(sum)]
    split Full Name into First and Last [on space]                  (split column)
    split City into rows sharing Usage [equally | keeping totals] [keeping Delhi NCR together]
    allocate Spend, Reach by City from Audience using Pct matching Campaign [as whole numbers]
    sum Impressions, Clicks by day, channel, network               count rows by A, B as orders
    label channel not in Channels as Others [also network] [then sum clicks, installs by day]
    net next-month negatives in Spend by Month per Site, Format
    append DAP New to DAP Base where DATE >= 2025-09-09 renaming DATE as Date, CITY_NAME as City
    look up CURRENT_AOV from AOV matching ORDER_DATE = ORDER_DATE, ORDER_CITY = CITY_NAME [in Orders]
    convert Spend from USD to INR at 83.2 | at 86.5 for Q1 2025, 85 for Q2 2025 using Date
    sort by Period, Geography [descending]                          make Date from Month and Day
    add Month as the month of Order Date                            drop columns A, B · drop table AOV
    set Tier to Gold if Spend > 100000, Silver if Spend > 50000, else Bronze
"""
from __future__ import annotations

import re
from datetime import date

from ..operations import (Aggregate, AllocateByShare, AppendRows, BucketOthers, Classify, ConvertCurrency, DropColumns,
                          DropTable, LookupJoin, MakeDate, MakePeriod, NetNextPeriod, Pivot, Plan, Rule, SortRows,
                          SplitColumn, SplitRows, Unpivot)

SEPARATOR_WORDS = {"space": " ", "spaces": " ", "comma": ",", "commas": ",", "dash": "-", "hyphen": "-", "slash": "/",
                   "semicolon": ";", "pipe": "|", "underscore": "_", "colon": ":", "dot": ".", "period": "."}


class TransformCommands:
    """Needs from the host: self.ctx (PlanningContext), self.column(), self.columns(), self.conditions(), Ask, and the
    helpers split_values / unquote / parse_number passed in at import time by parser.py."""

    # ── helpers ──────────────────────────────────────────────────────────────
    def _table_tail(self, text: str) -> tuple[str | None, str]:
        """'… in Coupons' -> ('Coupons', '…') when the words name a table; else (None, text)."""
        m = re.search(r"\s+(?:in|for|of|on|within)\s+(?:the\s+)?(?:table\s+|sheet\s+)?(?P<t>[^,]+?)(?:\s+(?:table|sheet))?$", text, re.I)
        if m:
            t = self.ctx.find_table(m.group("t"))
            if t is not None and self.ctx.find_column(m.group("t")) is None:
                return t, text[:m.start()].strip()
        return None, text

    def _table(self, name: str) -> str:
        t = self.ctx.find_table(name)
        if t is None:
            raise self.Ask(f"I could not find a table called '{self.unquote(name)}'. The tables are: {', '.join(self.ctx.table_names)}.")
        return t

    def _cols_in(self, spec: str, table: str | None) -> list[str]:
        """Columns by name, a range 'Day 1 to Day 31', or a list. Ranges follow the table's column order."""
        cols = self.ctx.columns_of(table)
        m = re.fullmatch(r"(?P<a>.+?)\s*(?:to|through|-|–|\.\.\.?|…)\s*(?P<b>.+)", spec.strip(), re.I)
        if m:
            a, b = self.ctx.find_column(m.group("a")), self.ctx.find_column(m.group("b"))
            if a in cols and b in cols:
                i, j = cols.index(a), cols.index(b)
                return cols[min(i, j):max(i, j) + 1]
        return self.columns(spec)

    def _pairs(self, spec: str) -> dict[str, str]:
        """'A = B, C = D' or 'A as B' -> {A: B}; a bare name means the same name on both sides."""
        out = {}
        for part in re.split(r"\s*,\s*(?:and\s+)?|\s+and\s+", spec.strip()):
            if not part:
                continue
            m = re.match(r"(?P<a>.+?)\s*(?:=|->|\bas\b|\bto\b|\bwith\b)\s*(?P<b>.+)$", part, re.I)
            a, b = (m.group("a"), m.group("b")) if m else (part, part)
            out[self.unquote(a.strip())] = self.unquote(b.strip())
        return out

    # ── unpivot / pivot ──────────────────────────────────────────────────────
    def _unpivot(self, t: str) -> Plan | None:
        m = re.match(r"(?:unpivot|melt|turn|convert|reshape|transpose)\s+(?:the\s+)?(?P<cols>.+?)\s+(?:columns?\s+)?(?:in)?to\s+(?:long\s+)?rows?"
                     r"(?:\s+(?:as|called|named|with|giving)\s+(?P<names>.+?))?$", t, re.I) \
            or re.match(r"(?:unpivot|melt)\s+(?:the\s+)?(?P<cols>.+?)(?:\s+(?:as|into|giving)\s+(?P<names>.+?))?$", t, re.I)
        if not m:
            return None
        table, cols_spec = self._table_tail(m.group("cols"))
        names = [self.unquote(x) for x in re.split(r"\s+and\s+|\s*,\s*|\s*/\s*", m.group("names") or "") if x.strip()]
        spec = re.sub(r"\s+columns?$", "", cols_spec.strip(), flags=re.I)
        prefix = re.fullmatch(r"(?:all\s+)?(?:the\s+)?(?:columns?\s+(?:starting|beginning)\s+with\s+)?['\"]?(?P<p>[A-Za-z][\w ]*?)['\"]?(?:\s*\*|\s+N|\s+columns?)?", spec, re.I)
        op_kw = {"table": table, "names_to": names[0] if names else None, "values_to": names[1] if len(names) > 1 else "Value"}
        cols = None
        try:
            cols = self._cols_in(spec, table)
        except self.Ask:
            if not prefix:
                raise
        if cols and len(cols) > 1:
            return Plan(ops=[Unpivot(columns=cols, **{k: v for k, v in op_kw.items() if v is not None},
                                     **({} if op_kw["names_to"] else {"names_to": _stem(cols)}))])
        p = (prefix.group("p").strip() if prefix else spec)
        pattern = rf"^{re.escape(p)}\s*\d+$"
        matched = [c for c in self.ctx.columns_of(table) if re.match(pattern, c, re.I)]
        if not matched:
            raise self.Ask(f"No columns look like '{p} 1', '{p} 2' …. Name the columns, e.g. 'unpivot Day 1 to Day 31'.")
        if table is None:
            having = [t.ref for t in self.ctx.tables if any(re.match(pattern, c, re.I) for c in t.cols)]
            op_kw["table"] = having[0] if len(having) == 1 else None
        return Plan(ops=[Unpivot(pattern=pattern, **{k: v for k, v in op_kw.items() if v is not None},
                                 **({} if op_kw["names_to"] else {"names_to": p}))])

    def _pivot(self, t: str) -> Plan | None:
        m = re.match(r"(?:pivot|spread|turn)\s+(?:the\s+)?(?:values\s+of\s+)?(?P<col>.+?)\s+(?:in)?to\s+columns?"
                     r"(?:\s+(?:using|with|holding|of|from|filled\s+with)\s+(?P<vals>.+?))?"
                     r"(?:\s+(?:by|per|for\s+each|keeping)\s+(?P<idx>.+?))?(?:\s*\(?(?P<agg>sum|mean|average|first|count)\)?)?$", t, re.I)
        if not m:
            return None
        table, col_spec = self._table_tail(m.group("col"))
        col = self.column(col_spec)
        if not m.group("vals"):
            raise self.Ask(f"Which column's numbers should fill the new {col} columns? e.g. 'pivot {col} into columns using Value'.")
        vals_spec = m.group("vals")
        if not table:
            table, vals_spec = self._table_tail(vals_spec)
        vals = self.column(vals_spec)
        idx = self.columns(m.group("idx")) if m.group("idx") else [c for c in self.ctx.columns_of(table) if c not in (col, vals)]
        agg = {"average": "mean"}.get((m.group("agg") or "sum").lower(), (m.group("agg") or "sum").lower())
        return Plan(ops=[Pivot(table=table, index=idx, columns_from=col, values_from=vals, agg=agg)])

    # ── split ────────────────────────────────────────────────────────────────
    def _split_rows(self, t: str) -> Plan | None:
        m = re.match(r"(?:split|explode|break)\s+(?:the\s+)?(?:rows\s+(?:by|on)\s+)?(?P<col>.+?)\s+(?:in)?to\s+(?:separate\s+|one\s+)?rows?(?P<rest>.*)$", t, re.I) \
            or re.match(r"(?:make\s+)?one\s+row\s+(?:per|for\s+each)\s+(?P<col>.+?)(?P<rest>(?:\s+(?:sharing|and|dividing|splitting|keeping|repeating).*)?)$", t, re.I)
        if not m:
            return None
        rest = m.group("rest") or ""
        table, col_spec = self._table_tail(m.group("col"))
        keep = re.search(r"\bkeeping\s+(?P<k>.+?)\s+(?:together|as\s+one)\b", rest, re.I)
        keep_together = self.split_values(keep.group("k")) if keep else []
        if keep:
            rest = rest[:keep.start()] + rest[keep.end():]
        method = "equal_integer"
        if re.search(r"\b(repeat|repeating|copy|copying)\b", rest, re.I):
            method = "repeat"
        elif re.search(r"\b(equally|evenly|as\s+decimals?)\b", rest, re.I) and not re.search(r"total|whole|integer", rest, re.I):
            method = "equal"
        sh = re.search(r"\b(?:sharing|share|and\s+share|dividing|divide|and\s+divide|splitting|allocating|repeating|copying)\s+(?P<c>.+?)"
                       r"(?:\s+(?:equally|evenly|keeping.*|as\s+.*|by\s+.*))?$", rest.strip(), re.I)
        if not table and sh:
            table, _ = self._table_tail(sh.group("c"))
        alloc = self.columns(re.sub(r"\s+(?:equally|evenly)$", "", self._table_tail(sh.group("c"))[1], flags=re.I)) if sh else []
        if not table:
            table, rest2 = self._table_tail(rest)
        return Plan(ops=[SplitRows(table=table, column=self.column(col_spec), allocate=alloc, method=method,
                                   keep_together=keep_together)])

    def _split_column(self, t: str) -> Plan | None:
        m = re.match(r"split\s+(?:the\s+)?(?:column\s+)?(?P<col>.+?)\s+into\s+(?P<into>.+?)(?:\s+(?:by|on|at|using|with|separated\s+by)\s+(?P<sep>.+))?$", t, re.I)
        if not m or re.search(r"\brows?\b", m.group("into"), re.I):
            return None
        col = self.ctx.find_column(m.group("col"))
        if col is None:
            return None
        into = self.split_values(m.group("into"))
        if len(into) < 2:
            raise self.Ask(f"Name at least two new columns, e.g. 'split {col} into First and Last'.")
        sep_raw = (m.group("sep") or "space").strip()
        word = re.sub(r"^(?:a|the|each)\s+", "", sep_raw.lower()).rstrip("s") if not re.match(r"['\"]", sep_raw) else None
        if word is not None and (word in SEPARATOR_WORDS or word + "s" in SEPARATOR_WORDS):
            sep = SEPARATOR_WORDS.get(word) or SEPARATOR_WORDS[word + "s"]
        else:
            sep = self.unquote(sep_raw)
        return Plan(ops=[SplitColumn(column=col, into=into, separator=sep)])

    def _allocate(self, t: str) -> Plan | None:
        m = re.match(r"(?:allocate|share|distribute|split)\s+(?P<cols>.+?)\s+(?:across|by|over)\s+(?P<cat>.+?)\s+from\s+(?P<tab>.+?)"
                     r"(?:\s+(?:using|by|with)\s+(?P<share>.+?))?\s+(?:matching|on|where|joining)\s+(?P<keys>.+?)(?P<int>\s+as\s+whole\s+numbers|\s+as\s+integers)?"
                     r"(?P<keep>\s+keeping\s+unmatched\s+\w+)?$", t, re.I)
        if not m:
            return None
        share_table = self._table(m.group("tab"))
        table = None
        cols_spec = m.group("cols")
        table, cols_spec = self._table_tail(cols_spec)
        pairs = self._pairs(m.group("keys"))
        if table is None:
            others = [n for n in self.ctx.table_names if n != share_table]
            table = others[0] if len(others) == 1 else None
        return Plan(ops=[AllocateByShare(table=table, share_table=share_table, on=pairs, category=self.unquote(m.group("cat")),
                                         share=self.unquote(m.group("share")) if m.group("share") else None,
                                         columns=self.columns(cols_spec), integer=bool(m.group("int")),
                                         keep_unmatched_shares=bool(m.group("keep")))])

    # ── grouping ─────────────────────────────────────────────────────────────
    def _aggregate(self, t: str) -> Plan | None:
        m = re.match(r"(?:count|number\s+of)\s+(?:the\s+)?(?:rows|records|orders|lines)?\s*(?:by|per|for\s+each)\s+(?P<by>.+?)\s+as\s+(?P<name>.+)$", t, re.I)
        if m:
            table, by = self._table_tail(m.group("by"))
            return Plan(ops=[Aggregate(table=table, by=self.columns(by), count_as=self.unquote(m.group("name")))])
        m = re.match(r"(?P<verb>sum|add\s+up|total|aggregate|average|mean)\s+(?:the\s+|up\s+)?(?P<cols>.+?)\s+(?:by|per|for\s+each|grouped\s+by)\s+(?P<by>.+?)$", t, re.I) \
            or re.match(r"group\s+(?:the\s+rows\s+)?by\s+(?P<by>.+?)\s+and\s+(?P<verb>sum|add\s+up|total|average)\s+(?P<cols>.+)$", t, re.I)
        if not m:
            return None
        table, by = self._table_tail(m.group("by"))
        if table is None:
            table, cols = self._table_tail(m.group("cols"))
        else:
            cols = m.group("cols")
        cols = self.columns(cols)
        mean = m.group("verb").lower() in ("average", "mean")
        return Plan(ops=[Aggregate(table=table, by=self.columns(by), **({"mean": cols} if mean else {"sum": cols}))])

    def _others(self, t: str) -> Plan | None:
        m = re.match(r"(?:label|group|bucket|put|mark|call|relabel|rename)\s+(?:the\s+)?(?:other\s+|unknown\s+|remaining\s+)?(?P<col>.+?)\s+(?:values\s+)?"
                     r"(?:(?:not\s+(?:in|on)\s+(?:the\s+)?(?:list\s+)?(?P<keep>.+?))|(?:outside\s+(?:the\s+)?top\s+(?P<top>\d+)))"
                     r"\s+(?:as|into|under)\s+(?P<label>\S+?|\"[^\"]+\"|'[^']+')(?P<rest>(?:\s+(?:also|and|then|,).*)?)$", t, re.I)
        if not m:
            m2 = re.match(r"keep\s+(?:the\s+)?top\s+(?P<top>\d+)\s+(?P<col>.+?)(?:,|\s+and)\s+(?:the\s+)?(?:others|rest)\s+(?:as|into)\s+(?P<label>\S+)(?P<rest>.*)$", t, re.I)
            if not m2:
                return None
            m = m2
        rest = m.group("rest") or ""
        table, col_spec = self._table_tail(m.group("col"))
        col = self.column(col_spec)
        keep, keep_from = None, None
        if m.groupdict().get("keep"):
            k = m.group("keep")
            ref = self.ctx.find_table(re.sub(r"\s+(?:table|sheet|list)$", "", k, flags=re.I))
            if ref is not None and ref != table:
                keep_from = {"table": ref, "column": col}
            else:
                keep = self.split_values(k)
        also = []
        ma = re.search(r"\balso\s+(?:set\s+|label\s+)?(?P<a>.+?)(?=\s+then\b|$)", rest, re.I)
        if ma:
            also = self.columns(ma.group("a"))
        sum_by, sums = None, []
        ms = re.search(r"\b(?:then\s+)?(?:sum|add\s+up|total)\s+(?P<s>.+?)\s+(?:by|per)\s+(?P<b>.+)$", rest, re.I)
        if ms:
            sums, sum_by = self.columns(ms.group("s")), self.columns(ms.group("b"))
        return Plan(ops=[BucketOthers(table=table, column=col, keep=keep, keep_from=keep_from,
                                      top_n=int(m.group("top")) if m.groupdict().get("top") else None,
                                      label=self.unquote(m.group("label")), also=also, sum_by=sum_by, sum=sums)])

    def _net(self, t: str) -> Plan | None:
        m = re.match(r"net\s+(?:the\s+|off\s+)?(?:next[- ]month(?:'s)?\s+)?(?:negative\s+)?(?:negatives\s+|returns?\s+|values?\s+)?(?:in|of|for)?\s*"
                     r"(?P<val>.+?)\s+(?:by|per|across)\s+(?P<per>.+?)(?:\s+(?:per|for\s+each|by|grouped\s+by)\s+(?P<by>.+))?$", t, re.I)
        if not m:
            return None
        table, val = self._table_tail(m.group("val"))
        val = re.sub(r"^(?:negatives?|returns?)\s+(?:in|of)\s+", "", val, flags=re.I)
        by = m.group("by") or ""
        if by and table is None:
            table, by = self._table_tail(by)
        return Plan(ops=[NetNextPeriod(table=table, value=self.column(val), period=self.column(m.group("per")),
                                       group_by=self.columns(by) if by else [])])

    # ── combining tables ─────────────────────────────────────────────────────
    def _append(self, t: str) -> Plan | None:
        m = re.match(r"(?:append|stack|add|put|combine)\s+(?:the\s+)?(?:rows\s+(?:of|from)\s+)?(?P<src>.+?)\s+(?:to|under|below|onto|into|after)\s+(?P<dst>.+?)"
                     r"(?:\s+(?:only\s+)?where\s+(?P<cond>.+?))?(?:\s+(?:renaming|mapping|with\s+columns?)\s+(?P<map>.+))?$", t, re.I)
        if not m:
            return None
        src, dst = self.ctx.find_table(m.group("src")), self.ctx.find_table(m.group("dst"))
        if src is None or dst is None:
            if src is None and dst is None:
                return None
            raise self.Ask(f"Which tables? The tables are: {', '.join(self.ctx.table_names)}.")
        rename = self._pairs(m.group("map")) if m.group("map") else {}
        conds = []
        if m.group("cond"):
            saved = self.ctx
            conds = self.conditions(m.group("cond"))
        return Plan(ops=[AppendRows(table=dst, source=src, rename=rename, where=conds)])

    def _lookup(self, t: str) -> Plan | None:
        m = re.match(r"(?:look\s*up|bring\s+in|fetch|get|join|pull\s+in|add)\s+(?P<cols>.+?)\s+from\s+(?P<tab>.+?)\s+(?:matching|on|by|where|using)\s+(?P<keys>.+?)"
                     r"(?:\s+(?:into|for|in)\s+(?P<dst>[^,]+?))?(?P<first>\s+\(?(?:take|keep|use)\s+the\s+first\)?)?$", t, re.I)
        if not m:
            return None
        lookup = self.ctx.find_table(m.group("tab"))
        if lookup is None:
            return None
        dst = self._table(m.group("dst")) if m.group("dst") else None
        pairs = self._pairs(m.group("keys"))
        from ..selectors import find_column
        for a, b in pairs.items():
            if self.ctx.find_column(a) is None:
                raise self.Ask(f"I could not find a column called '{a}' to match on.")
            if find_column(b, self.ctx.columns_of(lookup)) is None:
                raise self.Ask(f"{lookup} has no column '{b}'. Its columns are: {', '.join(self.ctx.columns_of(lookup))}.")
        if dst is None:
            others = [n for n in self.ctx.table_names if n != lookup]
            dst = others[0] if len(others) == 1 else None
        return Plan(ops=[LookupJoin(table=dst, lookup=lookup, on=pairs, columns=self.split_values(m.group("cols")),
                                    duplicates="first" if m.group("first") else "mean")])

    # ── values ───────────────────────────────────────────────────────────────
    def _currency(self, t: str) -> Plan | None:
        m = re.match(r"convert\s+(?P<cols>.+?)\s+from\s+(?P<a>[A-Za-z$€£₹]{1,3})\s+to\s+(?P<b>[A-Za-z$€£₹]{1,3})\s+(?:at|using|with\s+(?:a\s+)?rate(?:\s+of)?|rate)\s+(?P<rest>.+)$", t, re.I)
        if not m:
            return None
        table, cols = self._table_tail(m.group("cols"))
        rest = m.group("rest")
        note = f"{m.group('a').upper()} -> {m.group('b').upper()}"
        quarters = re.findall(r"([\d.,]+)\s+(?:for|in)\s+Q([1-4])\s*[-' ]?\s*(\d{2,4})", rest, re.I)
        if quarters:
            dm = re.search(r"\b(?:using|by|on|from)\s+(?:the\s+)?(?:date\s+(?:in\s+)?)?(?P<d>[^,]+?)(?:\s+column)?$", rest, re.I)
            if not dm:
                raise self.Ask("Which column holds the date that picks the rate? e.g. '… using Date'.")
            rates = []
            for rate, q, y in quarters:
                yy = int(y) + (2000 if len(y) == 2 else 0)
                lo = date(yy, 3 * int(q) - 2, 1)
                hi = date(yy + (q == "4"), 1 if q == "4" else 3 * int(q) + 1, 1)
                rates.append({"from": lo.isoformat(), "to": date.fromordinal(hi.toordinal() - 1).isoformat(),
                              "rate": float(rate.replace(",", ""))})
            return Plan(ops=[ConvertCurrency(table=table, columns=self.columns(cols), rates=rates,
                                             date_column=self.column(dm.group("d")), note=note)])
        rate = self.parse_number(rest.split()[0])
        if rate is None:
            raise self.Ask("What rate? e.g. 'convert Spend from USD to INR at 83.2', or per quarter: "
                           "'at 86.5 for Q1 2025, 85 for Q2 2025 using Date'.")
        return Plan(ops=[ConvertCurrency(table=table, columns=self.columns(cols), rate=rate, note=note)])

    def _sort(self, t: str) -> Plan | None:
        m = re.match(r"(?:sort|order|arrange)\s+(?:the\s+)?(?:rows\s+(?:of\s+)?)?(?:(?P<tab>.+?)\s+)?by\s+(?P<keys>.+)$", t, re.I)
        if not m:
            return None
        table = self._table(m.group("tab")) if m.group("tab") else None
        keys_spec = m.group("keys")
        if table is None:
            table, keys_spec = self._table_tail(keys_spec)
        by, desc = [], []
        for part in re.split(r"\s*,\s*(?:then\s+)?(?:and\s+)?|\s+then\s+|\s+and\s+", keys_spec):
            d = bool(re.search(r"\s+(?:desc(?:ending)?|newest\s+first|latest\s+first|largest\s+first|highest\s+first|z\s*-\s*a)$", part, re.I))
            name = re.sub(r"\s+(?:asc(?:ending)?|desc(?:ending)?|newest\s+first|latest\s+first|largest\s+first|highest\s+first|oldest\s+first|smallest\s+first|a\s*-\s*z|z\s*-\s*a)$", "", part, flags=re.I)
            if name.strip():
                by.append(self.column(name))
                desc.append(d)
        return Plan(ops=[SortRows(table=table, by=by, descending=desc)])

    def _make_date(self, t: str) -> Plan | None:
        m = re.match(r"(?:make|build|create|add|derive)\s+(?:a\s+|the\s+)?(?:(?P<name>\S+)\s+)?(?:date\s+)?(?:column\s+)?from\s+(?P<m>.+?)\s+and\s+(?P<d>.+)$", t, re.I)
        if not m or "date" not in t.lower():
            return None
        table, d = self._table_tail(m.group("d"))
        name = self.unquote(m.group("name")) if m.group("name") and m.group("name").lower() not in ("a", "the", "date") else "Date"
        return Plan(ops=[MakeDate(table=table, month=self.column(m.group("m")), day=self.column(d), name=name)])

    def _make_period(self, t: str) -> Plan | None:
        m = re.match(r"add\s+(?:a\s+)?(?:column\s+)?(?P<name>.+?)\s+(?:as|=|with)\s+(?:the\s+)?(?P<unit>month|quarter|year|week)\s+(?:of|from)\s+(?P<col>.+)$", t, re.I) \
            or re.match(r"add\s+the\s+(?P<unit>month|quarter|year|week)\s+of\s+(?P<col>.+?)\s+as\s+(?P<name>.+)$", t, re.I)
        if not m:
            return None
        table, col = self._table_tail(m.group("col"))
        return Plan(ops=[MakePeriod(table=table, column=self.column(col), unit=m.group("unit").lower(),
                                    name=self.unquote(m.group("name")))])

    def _drop(self, t: str) -> Plan | None:
        m = re.match(r"(?:drop|remove|exclude|leave\s+out)\s+(?:the\s+)?table\s+(?P<t>.+)$", t, re.I)
        if m:
            return Plan(ops=[DropTable(table=self._table(m.group("t")))])
        m = re.match(r"(?:drop|remove|delete)\s+(?:the\s+)?columns?\s+(?P<c>.+?)$", t, re.I)
        if m:
            table, cols = self._table_tail(m.group("c"))
            return Plan(ops=[DropColumns(table=table, columns=self.columns(cols))])
        return None

    def _classify(self, t: str) -> Plan | None:
        m = re.match(r"(?:set|make|add(?:\s+(?:a\s+)?column)?|create(?:\s+(?:a\s+)?column)?|classify)\s+(?P<col>[^,]+?)\s*(?:to|=|as|:)?\s+(?P<body>[^,]+?\s+if\s+.+)$", t, re.I)
        if not m:
            return None
        body = m.group("body")
        default, keep = None, False
        md = re.search(r"[,;]?\s*(?:else|otherwise)\s+(?P<d>.+)$", body, re.I)
        if md:
            d = md.group("d").strip()
            body = body[:md.start()]
            if re.fullmatch(r"(?:keep|leave)\s+(?:it|as\s+is|the\s+(?:current|existing)\s+value)", d, re.I):
                keep = True
            else:
                default = None if d.lower() in ("empty", "blank", "null", "nothing") else self.unquote(d)
        rules = []
        for part in re.split(r"\s*[,;]\s*(?=[^,;]+?\s+if\s+)", body.strip()):
            pm = re.match(r"(?P<v>.+?)\s+if\s+(?P<c>.+)$", part.strip(), re.I)
            if not pm:
                return None
            rules.append(Rule(value=self.unquote(pm.group("v").strip()), when=self.conditions(pm.group("c"))))
        table, col = self._table_tail(m.group("col"))
        return Plan(ops=[Classify(column=self.unquote(col.strip()), rules=rules, default=default, keep_existing=keep,
                                  tables=[table] if table else None)])


COLUMN_FIELDS = ("column", "columns", "by", "sum", "mean", "first", "month", "day", "period", "group_by", "index",
                 "columns_from", "values_from", "allocate")


def infer_table(op, ctx) -> None:
    """Phase T: a step that names no table goes to the one table that has every column it uses (if exactly one)."""
    if getattr(op, "table", "x") is not None or ctx.phase != "T" or len(ctx.tables) < 2:
        return
    used: list[str] = []
    for f in COLUMN_FIELDS:
        v = getattr(op, f, None)
        if op.op == "net_next_period" and f == "column":
            continue
        used += [v] if isinstance(v, str) else list(v or []) if isinstance(v, list) else []
    if op.op == "net_next_period":
        used.append(op.value)
    if not used:
        return
    from src.hygiene.cities import normalize
    hits = [t.ref for t in ctx.tables if all(any(normalize(c) == normalize(x) for x in t.cols) for c in used)]   # exact names
    if len(hits) == 1:
        op.table = hits[0]


def _stem(cols: list[str]) -> str:
    """'Day 1'..'Day 31' -> 'Day': the shared leading word(s) of the columns, else 'Variable'."""
    first = re.match(r"^(.*?)[\s_-]*\d+$", cols[0])
    if first and all(c.startswith(first.group(1)) for c in cols) and first.group(1).strip():
        return first.group(1).strip()
    return "Variable"
