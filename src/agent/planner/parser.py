"""The deterministic planner: plain-English commands an analyst types most often, turned into typed operations.

It runs before the model on every message. It is fast, needs no GPU, gives the same plan for the same words, and
uses the data to decide what words mean (is 'Head Office' a value in City? is 'Spend' a column?). When it is not
sure it returns None, and the LLM planner (if available) gets the message. It never guesses a column or a value:
a name it cannot place becomes a question back to the analyst.

Commands it understands (wording is flexible; see tests/test_agent_parser.py for the full set):
    accept | reject | keep  [all] [the] <rule or topic> [fixes] [in <column>] [on rows ..] [except rows ..]
    change|replace|map <values> to <value> [in <column>] [everywhere]        <values> is <value>   (D-26 buckets)
    clear|blank <values> in <column>                remove '<text>' from <column>
    set <column> to <value> [where <condition>] [on rows ..]                  in row 14 set City to Delhi
    fill empty <column> with <value>
    multiply|divide <columns> by <n>                <columns> is in thousands | lakhs | millions | crores
    add column <name> = <formula> | with value <v> | from <column>: a=b, c=d
    rename column <a> to <b>                        remove rows where <condition> | remove rows 4, 7
    split the multi-value cells [in <column>]       summary | help
Conditions: <column> is <v> | is not <v> | is one of a, b | contains <v> | starts with <v> | is empty |
            > < >= <= <n>, joined with 'and'.
"""
from __future__ import annotations

import re

from src.hygiene.cities import normalize
from src.hygiene.config import RULE_TEXT

from ..operations import (AddColumn, Condition, Plan, RemoveRows, RenameColumn, ReplaceText, ReplaceValues,
                          ResolveFindings, RowFilter, Scale, SetValue)
from .context import PlanningContext, rules_for_words
from .transform_commands import TransformCommands, infer_table

EMPTY_WORDS = {"empty", "blank", "null", "nothing", "none", "missing", "na", "n/a", "nan", "no value", "an empty cell",
               "empty cell", "blank cell"}
UNITS = {"k": 1e3, "thousand": 1e3, "thousands": 1e3, "'000": 1e3, "000s": 1e3, "lakh": 1e5, "lakhs": 1e5, "lac": 1e5,
         "lacs": 1e5, "million": 1e6, "millions": 1e6, "mn": 1e6, "m": 1e6, "crore": 1e7, "crores": 1e7, "cr": 1e7,
         "billion": 1e9, "billions": 1e9, "bn": 1e9}
FILLER = r"(?:all|the|every|each|of|any|open|remaining|these|those|items?|fix(?:es)?|changes?|issues?|flags?|findings?|" \
         r"suggestions?|proposals?|proposed|values?|automatic|auto|review|cells?|rows?)"
CODE_RE = re.compile(r"\b([DNES])\s*-?\s*(\d{2})\b", re.I)

HELP = """I can change this phase's data for you. Each change is shown as a preview first and is only applied when you click Apply. For example:
• "accept all date fixes except row 14"  •  "reject the outlier fixes in Spend"  •  "keep row 33"
• "Head Office is Bangalore"  •  "change Sedan, Hatchback and 4dr Sedan to Passenger Car in Vehicle"
• "TBD and TBC in Status mean empty"  •  "remove '$' from Spend"
• "set Region to APAC where Country is India"  •  "in row 14 set City to Delhi"  •  "fill empty Channel with Unknown"
• "Spend is in thousands"  •  "divide Revenue by 100"
• "add column Campaign_Type from Campaign: Diwali Blast = Festive, Summer Sale = Seasonal"
• "add column Revenue = Orders * AOV"  •  "rename column Amt to Amount"
• "remove rows where Status is Test"  •  "split the multi-value cells in City"
Ask "summary" to see what is still open.

In Transformations (Phase T) the chat also reshapes tables:
• "unpivot Day 1 to Day 31 into rows as Day and Reactivated"  •  "make Date from Month and Day"
• "pivot Inactivity Bucket into columns using Reactivated"  •  "split Full Name into First and Last"
• "split City into rows sharing Usage keeping Delhi NCR together"  •  "sum Impressions, Clicks by day, channel, network"
• "label channel not in Channels as Others also network then sum clicks by day"
• "allocate Spend, Reach by City from Audience using Pct matching Campaign"
• "net next-month negatives in Spend by Month per Site"  •  "append DAP New to DAP Base where DATE >= 2025-09-09"
• "look up CURRENT_AOV from AOV matching Date, City"  •  "convert Spend from USD to INR at 83.2"
• "set Tier to Gold if Spend > 100000, else Silver"  •  "sort by Period, Geography"  •  "drop table AOV"
• "save recipe as Coupons monthly"  •  "use recipe Coupons monthly"  •  "show recipe"
"""


class Ask(Exception):
    """The command is clear but a name in it is not: ask the analyst instead of guessing."""


# ── small parsers ───────────────────────────────────────────────────────────
def clean(text: str) -> str:
    t = text.strip()
    for a, b in (("“", '"'), ("”", '"'), ("‘", "'"), ("’", "'"), ("→", "->"), ("⇒", "->"), ("–", "-"), ("—", "-")):
        t = t.replace(a, b)
    t = re.sub(r"\s+", " ", t)
    t = re.sub(r"^(please|pls|kindly|can you|could you|now|ok|okay|and|also|then)\b[\s,]*", "", t, flags=re.I)
    t = re.sub(r"^(please|pls|kindly)\b[\s,]*", "", t, flags=re.I)
    return t.rstrip(" .!?").strip()


def unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'`":
        return s[1:-1]
    return s


def split_values(s: str) -> list[str]:
    """'Sedan, Hatchback and "4dr Sedan"' -> ['Sedan', 'Hatchback', '4dr Sedan'] (quoted parts are kept whole)."""
    quoted = re.findall(r'"([^"]*)"|\'([^\']*)\'', s)
    if quoted and re.fullmatch(r'\s*(?:(?:"[^"]*"|\'[^\']*\')\s*(?:,|\band\b|\bor\b|/|&)?\s*)+', s):
        return [a or b for a, b in quoted]
    parts = re.split(r"\s*,\s*(?:and\s+|or\s+)?|\s+and\s+|\s+or\s+|\s*/\s*|\s*&\s*", s.strip())
    return [unquote(p) for p in parts if unquote(p)]


def parse_rows(s: str) -> list[int]:
    out: list[int] = []
    for m in re.finditer(r"(\d+)\s*(?:-|to|through)\s*(\d+)|(\d+)", s):
        lo, hi = (int(m.group(1)), int(m.group(2))) if m.group(1) else (int(m.group(3)), int(m.group(3)))
        if hi - lo > 100000:
            raise Ask("That row range is too large; name the rows or use a condition.")
        out += list(range(lo, hi + 1))
    return list(dict.fromkeys(out))


def parse_number(s: str) -> float | None:
    s = s.strip().lower().replace(",", "")
    m = re.fullmatch(r"(\d+(?:\.\d+)?)\s*(k|thousand|lakh|lakhs|lac|million|mn|m|crore|cr|billion|bn)?", s)
    if not m:
        return None
    return float(m.group(1)) * UNITS.get(m.group(2) or "", 1)


def as_value(s: str) -> str | None:
    v = unquote(s.strip())
    return None if v.lower() in EMPTY_WORDS else v


class CommandParser(TransformCommands):
    Ask = Ask

    def __init__(self, ctx: PlanningContext):
        self.ctx = ctx

    split_values = staticmethod(lambda s: split_values(s))
    unquote = staticmethod(lambda s: unquote(s))
    parse_number = staticmethod(lambda s: parse_number(s))

    # ── entry ────────────────────────────────────────────────────────────────
    def parse(self, text: str) -> Plan | None:
        t = clean(text)
        if not t:
            return Plan(answer=HELP)
        try:
            for handler in (self._help, self._summary, self._undo, self._drop, self._remove_rows, self._classify,
                            self._unpivot, self._pivot, self._split_column, self._split_rows, self._allocate, self._split,
                            self._rename, self._make_period, self._make_date, self._aggregate, self._others, self._net,
                            self._append, self._lookup, self._currency, self._sort, self._add_column, self._scale,
                            self._resolve, self._fill_empty, self._row_set, self._set, self._set_no_to, self._clear,
                            self._replace, self._statement):
                plan = handler(t)
                if plan is not None:
                    for op in plan.ops:
                        infer_table(op, self.ctx)
                    return plan
        except Ask as q:
            return Plan(ask=str(q))
        return None

    # ── names ────────────────────────────────────────────────────────────────
    def column(self, name: str, required: bool = True) -> str | None:
        col = self.ctx.find_column(name)
        if col is None and required:
            cols = self.ctx.columns
            raise Ask(f"I could not find a column called '{unquote(name)}'. The columns are: {', '.join(cols[:40])}"
                      + (" ..." if len(cols) > 40 else "") + ".")
        return col

    def columns(self, spec: str) -> list[str]:
        whole = self.column(spec, required=False)
        if whole:
            return [whole]
        return [self.column(p) for p in split_values(spec)]

    # ── where clauses ────────────────────────────────────────────────────────
    def conditions(self, spec: str) -> list[Condition]:
        out = []
        for part in re.split(r"\s+and\s+(?=[\w\"'\[])", spec):
            out.append(self.condition(part))
        return out

    def condition(self, s: str) -> Condition:
        s = s.strip()
        pats = [
            (r"(?P<c>.+?)\s+(?:is|are)\s+(?:not\s+empty|not\s+blank|filled|present)$", "not_empty", None),
            (r"(?P<c>.+?)\s+(?:is|are)\s+(?:empty|blank|missing|null)$", "empty", None),
            (r"(?P<c>.+?)\s+(?:is|are)\s+not\s+one\s+of\s+(?P<v>.+)$", "not_in", "list"),
            (r"(?P<c>.+?)\s+(?:is|are)\s+(?:one\s+of|in)\s+(?P<v>.+)$", "in", "list"),
            (r"(?P<c>.+?)\s+(?:>=|is\s+at\s+least)\s*(?P<v>.+)$", "gte", "num"),
            (r"(?P<c>.+?)\s+(?:<=|is\s+at\s+most)\s*(?P<v>.+)$", "lte", "num"),
            (r"(?P<c>.+?)\s*(?:>|\s+is\s+(?:greater|more|bigger|higher)\s+than|\s+(?:is\s+)?above|\s+(?:is\s+)?over)\s*(?P<v>.+)$", "gt", "num"),
            (r"(?P<c>.+?)\s*(?:<|\s+is\s+(?:less|smaller|lower)\s+than|\s+(?:is\s+)?below|\s+(?:is\s+)?under)\s*(?P<v>.+)$", "lt", "num"),
            (r"(?P<c>.+?)\s+(?:contains|has|includes)\s+(?P<v>.+)$", "contains", "text"),
            (r"(?P<c>.+?)\s+(?:starts|begins)\s+with\s+(?P<v>.+)$", "starts_with", "text"),
            (r"(?P<c>.+?)\s+ends\s+with\s+(?P<v>.+)$", "ends_with", "text"),
            (r"(?P<c>.+?)\s+(?:is\s+not|isn't|!=|<>|not\s+equal\s+to)\s+(?P<v>.+)$", "ne", "text"),
            (r"(?P<c>.+?)\s*(?:\s+is|\s+equals|==|=|\s+was)\s+(?P<v>.+)$", "eq", "text"),
        ]
        for pat, op, kind in pats:
            m = re.match(pat, s, re.I)
            if not m:
                continue
            col = self.ctx.find_column(m.group("c"))
            if col is None:
                continue
            if kind is None:
                return Condition(column=col, op=op)
            raw = m.group("v").strip()
            if kind == "num":
                n = parse_number(raw)
                if n is None:
                    from ..selectors import as_date
                    if as_date(unquote(raw)) is not None:                    # 'Date >= 2025-09-09' compares dates
                        return Condition(column=col, op=op, value=unquote(raw))
                    continue
                return Condition(column=col, op=op, value=n)
            if kind == "list":
                return Condition(column=col, op=op, value=split_values(raw))
            return Condition(column=col, op=op, value=unquote(raw))
        raise Ask(f"I could not read the condition '{s}'. Write it like: City is Delhi, Spend > 1000, Status is empty.")

    def where(self, rest: str) -> tuple[RowFilter, str]:
        """Peel trailing where / rows / except / file clauses off `rest`; returns the filter and what is left."""
        f = RowFilter()
        rest = rest.strip()
        while True:
            m = re.search(r"\s*\bexcept\s+(?:for\s+)?(?:excel\s+)?(?:rows?|lines?)?\s*(?P<r>[\d,\s\-and to]+)$", rest, re.I)
            if m and parse_rows(m.group("r")):
                f.exclude_rows = parse_rows(m.group("r"))
                rest = rest[:m.start()].strip()
                continue
            m = re.search(r"\s*\b(?:in|on|for|at)?\s*(?:excel\s+)?(?:rows?|lines?)\s+(?P<r>\d[\d,\s\-]*(?:\s*(?:and|to)\s*\d+)*)$", rest, re.I)
            if m:
                f.rows = parse_rows(m.group("r"))
                rest = rest[:m.start()].strip()
                continue
            m = re.search(r"\s*\b(?:in|for|from)\s+(?:the\s+)?(?:file|sheet|table)\s+(?P<t>.+)$", rest, re.I)
            if m:
                f.tables = [unquote(m.group("t"))]
                rest = rest[:m.start()].strip()
                continue
            m = re.search(r"\s*\b(?:where|when|if|for rows where|for which|whose)\s+(?P<c>.+)$", rest, re.I)
            if m:
                f.conditions = self.conditions(m.group("c"))
                rest = rest[:m.start()].strip()
                continue
            return f, rest

    # ── commands ─────────────────────────────────────────────────────────────
    def _help(self, t: str) -> Plan | None:
        if re.fullmatch(r"(help|\?|what can you do|how does this work|commands|examples?|what can i (say|ask|do))", t, re.I):
            return Plan(answer=HELP)
        return None

    def _summary(self, t: str) -> Plan | None:
        m = re.fullmatch(r"(?:explain|what is|what's|what does|describe)\s+(?P<code>[DNES]\s*-?\s*\d{2})(?:\s+mean|\s+do)?", t, re.I)
        if m:
            code = CODE_RE.sub(lambda x: f"{x.group(1).upper()}-{x.group(2)}", m.group("code"))
            groups = [g for g in self.ctx.finding_groups() if g.rule == code]
            text = f"{code}: {RULE_TEXT.get(code, 'no description')}."
            if groups:
                text += " Open here: " + "; ".join(f"{g.count} in {g.column or 'the table'}" for g in groups) + "."
            return Plan(answer=text)
        if not re.fullmatch(r"(summary|status|overview|what'?s left|what is left|what remains|what'?s open|"
                            r"(show|list)( me)?( the| all)?( open| remaining)? (issues|items|findings|problems|flags)|"
                            r"what (are|is) (the )?(open |remaining )?(issues|problems|items|findings)|"
                            r"how many (issues|items|findings)( are)?( left| open)?)", t, re.I):
            return None
        groups = self.ctx.finding_groups()
        if not groups:
            return Plan(answer="Nothing is open: every review item has an answer. You can accept the phase.")
        lines = [f"{sum(g.count for g in groups)} review item(s) are open:"]
        for g in groups[:15]:
            ex = f" (e.g. {', '.join(g.examples)})" if g.examples else ""
            lines.append(f"• {g.rule} {g.column or '(table)'}: {g.count} — {RULE_TEXT.get(g.rule, '')}{ex}")
        if len(groups) > 15:
            lines.append(f"… and {len(groups) - 15} more groups.")
        return Plan(answer="\n".join(lines))

    def _undo(self, t: str) -> Plan | None:
        if re.fullmatch(r"(undo|undo (that|it|the last( change)?)|revert (that|it|the last change)|go back)", t, re.I):
            return Plan(answer="To undo the last applied change, click “Undo last change” above the chat. Changes that "
                               "are only proposed can be discarded with their Discard button.")
        return None

    def _remove_rows(self, t: str) -> Plan | None:
        m = re.match(r"(?:remove|delete|drop|exclude)\s+(?:the\s+|all\s+|these\s+)?(?:rows?|records?|lines?|entries)\b\s*(?P<rest>.*)$", t, re.I)
        if not m:
            return None
        rest = m.group("rest")
        if re.fullmatch(r"\d[\d,\s\-and to]*", rest.strip()):
            return Plan(ops=[RemoveRows(where=RowFilter(rows=parse_rows(rest)))])
        f, left = self.where(rest)
        if left.strip():
            f2, _ = self.where("where " + left)
            f.conditions += f2.conditions
        if not (f.rows or f.conditions):
            raise Ask("Which rows should I remove? Give Excel row numbers or a condition, e.g. 'remove rows where Status is Test'.")
        return Plan(ops=[RemoveRows(where=f)])

    def _split(self, t: str) -> Plan | None:
        m = re.match(r"split\b(?P<rest>.*)$", t, re.I)
        if not m:
            return None
        rest = m.group("rest")
        f, rest = self.where(rest)
        col = None
        mc = re.search(r"\b(?:in|of|for)\s+(?:the\s+)?(?:column\s+)?(?P<c>.+?)(?:\s+column)?$", rest, re.I)
        if mc:
            col = self.column(mc.group("c"))
        return Plan(ops=[ResolveFindings(decision="split", rules=["D-29"], column=col, where=f)])

    def _rename(self, t: str) -> Plan | None:
        m = re.match(r"rename\s+(?:the\s+)?(?:column|header|field)\s+(?P<a>.+?)\s+(?:to|as|into)\s+(?P<b>.+)$", t, re.I) \
            or re.match(r"rename\s+(?P<a>.+?)\s+(?:to|as|into)\s+(?P<b>.+)$", t, re.I)
        if not m:
            return None
        col = self.ctx.find_column(m.group("a"))
        if col is None:
            if re.match(r"rename\s+(?:the\s+)?(?:column|header|field)", t, re.I):
                self.column(m.group("a"))
            return None                                   # 'rename Banglore to Bangalore' is a value change
        # 'rename column CITY_NAME to City in AOV': the tail names a table, it is not part of the new name
        table, new_name = None, m.group("b")
        tail = re.search(r"\s+in\s+(?:the\s+)?(?:table\s+|sheet\s+)?(?P<t>[^,]+?)(?:\s+(?:table|sheet))?$", new_name, re.I)
        if tail:                                           # only when that table really has the column being renamed
            named = self.ctx.find_table(tail.group("t"))
            if named is not None and col in self.ctx.columns_of(named):
                table, new_name = named, new_name[:tail.start()]
        return Plan(ops=[RenameColumn(column=col, new_name=unquote(new_name.strip()), tables=[table] if table else None)])

    def _add_column(self, t: str) -> Plan | None:
        m = re.match(r"(?:add|create|insert|make)\s+(?:a\s+)?(?:new\s+)?(?:column|field)\s+(?:called\s+|named\s+)?"
                     r"(?P<name>\"[^\"]+\"|'[^']+'|\S+)\s*(?P<rest>.*)$", t, re.I)
        if not m:
            return None
        name, rest = unquote(m.group("name")), m.group("rest").strip()
        f, rest = self.where(rest) if re.search(r"\b(?:in|for)\s+(?:the\s+)?(?:file|sheet|table)\b", rest, re.I) else (RowFilter(), rest)
        mm = re.match(r"(?:mapped\s+|mapping\s+|looked\s+up\s+)?from\s+(?P<src>.+?)\s*(?::|using|with|where|as)\s*(?P<pairs>.+)$", rest, re.I)
        if mm:
            src = self.column(mm.group("src"))
            mapping, default = {}, None
            for pair in re.split(r"\s*[,;]\s*(?:and\s+)?|\s+and\s+(?=\S+\s*(?:=|->|is|means)\s)", mm.group("pairs")):
                pm = re.match(r"(?P<k>.+?)\s*(?:=|->|:|\bis\b|\bmeans\b|\bbecomes\b)\s*(?P<v>.+)$", pair.strip(), re.I)
                if not pm:
                    raise Ask(f"I could not read '{pair}' as a mapping. Write pairs like: Diwali Blast = Festive, Summer Sale = Seasonal.")
                k = unquote(pm.group("k"))
                if k.lower() in ("others", "other", "everything else", "rest", "default", "anything else"):
                    default = as_value(pm.group("v"))
                else:
                    mapping[k] = as_value(pm.group("v"))
            return Plan(ops=[AddColumn(name=name, from_column=src, mapping=mapping, default=default, tables=f.tables)])
        mv = re.match(r"(?:=|as|with\s+(?:the\s+)?(?:value|formula)?|filled\s+with|set\s+to|equal\s+to|equals)\s*(?P<expr>.+)$", rest, re.I)
        if not mv:
            raise Ask(f"What should '{name}' hold? For example: 'add column {name} with value X', "
                      f"'add column {name} = Orders * AOV', or 'add column {name} from Campaign: A = X, B = Y'.")
        expr = mv.group("expr").strip()
        if re.search(r"[+\-*/]", expr) and any(re.search(rf"(?<!\w){re.escape(c)}(?!\w)", expr, re.I) for c in self.ctx.columns):
            return Plan(ops=[AddColumn(name=name, formula=expr, tables=f.tables)])
        return Plan(ops=[AddColumn(name=name, value=as_value(expr) or "", tables=f.tables)])

    def _scale(self, t: str) -> Plan | None:
        m = re.match(r"(?:multiply|scale)\s+(?P<cols>.+?)\s+by\s+(?P<f>[\d.,]+\s*\w*)(?P<rest>.*)$", t, re.I)
        factor = None
        if m:
            factor = parse_number(m.group("f"))
        else:
            m = re.match(r"divide\s+(?P<cols>.+?)\s+by\s+(?P<f>[\d.,]+\s*\w*)(?P<rest>.*)$", t, re.I)
            if m:
                n = parse_number(m.group("f"))
                factor = 1 / n if n else None
            else:
                m = re.match(r"(?:the\s+)?(?P<cols>.+?)\s+(?:is|are|was|were)\s+(?:in|reported\s+in|given\s+in|shown\s+in)\s+"
                             r"(?P<f>thousands?|k|'000s?|000s|lakhs?|lacs?|millions?|mn|crores?|cr|billions?|bn)\b(?P<rest>.*)$", t, re.I)
                if m:
                    unit = m.group("f").lower()
                    factor = UNITS.get(unit) or UNITS.get(unit.rstrip("s"))
        if not m:
            return None
        if not factor:
            raise Ask(f"I could not read '{m.group('f')}' as a number.")
        f, left = self.where(m.group("rest"))
        cols_spec = m.group("cols").strip()
        if re.fullmatch(r"(?:this|the|all)?\s*(?:file|sheet|data|table|numbers|amounts|values)", cols_spec, re.I):
            numeric = [c for c in self.ctx.columns if self.ctx.column_kind(c) == "numeric"]
            raise Ask(f"Which columns are in that unit? The number columns are: {', '.join(numeric) or 'none'}.")
        cols = self.columns(cols_spec)
        return Plan(ops=[Scale(columns=cols, factor=factor, where=f)])

    def _resolve(self, t: str) -> Plan | None:
        m = re.match(r"(?P<verb>accept|approve|apply|confirm|ok|reject|revert|restore|undo|keep|ignore|dismiss|skip|"
                     r"leave|close|resolve)\b(?P<rest>.*)$", t, re.I)
        if not m:
            return None
        verb, rest = m.group("verb").lower(), m.group("rest").strip()
        decision = {"accept": "accept", "approve": "accept", "apply": "accept", "confirm": "accept", "ok": "accept",
                    "reject": "reject", "revert": "reject", "restore": "reject", "undo": "reject"}.get(verb, "keep")
        if re.search(r"\b(?:original|old|previous)\s+values?\b", rest, re.I) and verb in ("keep", "leave"):
            decision = "reject"
            rest = re.sub(r"\b(?:the\s+)?(?:original|old|previous)\s+values?\b", "", rest, flags=re.I)
        if re.match(r"\s*(?:it|as\s+is|them|that|this)\s*$", rest, re.I) or (verb in ("keep", "leave") and re.fullmatch(r"(?:it\s+)?as\s+(?:it\s+)?is", rest.strip(), re.I)):
            rest = ""
        f, rest = self.where(rest)
        col = None
        mc = re.search(r"\b(?:in|for|on|of)\s+(?:the\s+)?(?:column\s+)?(?P<c>[^,]+?)(?:\s+column)?$", rest, re.I)
        if mc and self.ctx.find_column(mc.group("c")):
            col = self.ctx.find_column(mc.group("c"))
            rest = rest[:mc.start()]
        codes = [f"{a.upper()}-{b}" for a, b in CODE_RE.findall(rest)]
        rest_words = CODE_RE.sub(" ", rest)
        codes += [c for c in rules_for_words(rest_words) if c not in codes]
        leftover = re.sub(rf"\b{FILLER}\b|[,]|\band\b|\beverything\b|\bas\s+is\b|\bit\b", " ", rest_words, flags=re.I).strip()
        if leftover and not codes and not col:
            if verb in ("apply", "ok", "confirm", "undo", "close", "resolve", "restore", "skip", "leave"):
                return None                                  # 'apply the mapping…', 'restore the file': not a review answer
            col = self.column(leftover)                      # 'accept City' -> asks when City is not a column
        everything = bool(re.search(r"\b(all|everything|every)\b", rest, re.I))
        if not (codes or col or f.rows or f.conditions or everything):
            groups = self.ctx.finding_groups()
            listed = "; ".join(f"{g.rule} in {g.column or 'the table'} ({g.count})" for g in groups[:8])
            raise Ask(f"Which items should I {verb}? Name a rule, a column or rows, or say 'all'. Open now: {listed or 'none'}.")
        if codes:
            open_rules = set(self.ctx.open_rules())
            if not open_rules & set(codes):
                raise Ask(f"There is no open item for {', '.join(codes)}. Open rules: {', '.join(sorted(open_rules)) or 'none'}.")
            codes = [c for c in codes if c in open_rules]
        return Plan(ops=[ResolveFindings(decision=decision, rules=codes or None, column=col, where=f)])

    def _fill_empty(self, t: str) -> Plan | None:
        m = re.match(r"fill\s+(?:in\s+)?(?:the\s+|all\s+)?(?:empty|blank|missing)\s+(?:cells\s+|values\s+)?(?:in\s+|of\s+)?(?:the\s+)?"
                     r"(?P<c>.+?)\s+(?:with|as|by)\s+(?P<v>.+)$", t, re.I)
        if not m:
            return None
        rest_v = m.group("v")
        f, rest_v = self.where(rest_v)
        col = self.column(m.group("c"))
        f.conditions = [Condition(column=col, op="empty"), *f.conditions]
        return Plan(ops=[SetValue(column=col, value=as_value(rest_v), where=f)])

    def split_column_value(self, rest: str) -> tuple[str, str] | None:
        """'Region APAC' or 'Region to APAC' -> ('Region', 'APAC'): the longest leading column name, then the value."""
        words = rest.split()
        for n in range(len(words) - 1, 0, -1):
            prefix = unquote(" ".join(words[:n]))
            col = next((c for c in self.ctx.columns if normalize(c) == normalize(prefix)), None)   # exact name only
            if col is not None:
                value = re.sub(r"^(?:to|=|as|with|should\s+be|is|becomes)\s+", "", " ".join(words[n:]), flags=re.I)
                return (col, value) if value else None
        return None

    def _row_set(self, t: str) -> Plan | None:
        m3 = re.match(r"(?:set|make|change|update)\s+(?:the\s+)?(?P<rest>.+?)\s+(?:for|in|on|at)\s+(?:excel\s+)?rows?\s+"
                      r"(?P<r>\d[\d,\s\-]*(?:\s*(?:and|to)\s*\d+)*)$", t, re.I)
        if m3:
            cv = self.split_column_value(m3.group("rest"))
            if cv:
                return Plan(ops=[SetValue(column=cv[0], value=as_value(cv[1]), where=RowFilter(rows=parse_rows(m3.group("r"))))])
            named = re.match(r"(?P<c>.+?)\s+(?:to|=|as)\s+.+$", m3.group("rest"), re.I)
            if named:
                col = self.column(named.group("c"))                 # asks when it is not a column
                value = m3.group("rest")[named.end("c"):].strip()
                value = re.sub(r"^(?:to|=|as)\s+", "", value, flags=re.I)
                return Plan(ops=[SetValue(column=col, value=as_value(value), where=RowFilter(rows=parse_rows(m3.group("r"))))])
        m = re.match(r"(?:in\s+|on\s+|for\s+|at\s+)?(?:excel\s+)?rows?\s+(?P<r>\d[\d,\s\-]*(?:\s*(?:and|to)\s*\d+)*)\s*[,:]?\s*"
                     r"(?:set\s+|change\s+|make\s+|update\s+)?(?:the\s+)?(?P<c>.+?)\s+(?:to|=|should\s+be|is|as|becomes)\s+(?P<v>.+)$", t, re.I)
        if not m:
            m2 = re.match(r"(?:set|change|make|update)\s+(?:the\s+)?(?P<c>.+?)\s+(?:in|on|for|at)\s+(?:excel\s+)?rows?\s+"
                          r"(?P<r>\d[\d,\s\-]*(?:\s*(?:and|to)\s*\d+)*)\s+(?:to|=|as)\s+(?P<v>.+)$", t, re.I)
            if not m2:
                return None
            m = m2
        col = self.column(m.group("c"))
        return Plan(ops=[SetValue(column=col, value=as_value(m.group("v")), where=RowFilter(rows=parse_rows(m.group("r"))))])

    def _set(self, t: str) -> Plan | None:
        m = re.match(r"(?:set|make|update|fill)\s+(?:the\s+)?(?:column\s+)?(?P<c>.+?)\s+(?:to|=|as|with)\s+(?P<v>.+)$", t, re.I)
        if not m:
            return None
        col = self.ctx.find_column(m.group("c"))
        if col is None:
            if re.match(r"set\b", t, re.I) and not self.ctx.columns_with_value(m.group("c")):
                raise Ask(f"I could not find a column called '{unquote(m.group('c'))}'. The columns are: "
                          f"{', '.join(self.ctx.columns[:40])}. To replace a value instead, say: change "
                          f"'{unquote(m.group('c'))}' to '{unquote(m.group('v'))}'.")
            return None
        f, value = self.where(m.group("v"))
        return Plan(ops=[SetValue(column=col, value=as_value(value), where=f)])

    def _set_no_to(self, t: str) -> Plan | None:
        """'make Region APAC where Channel is TV' (no 'to'): the longest leading column name, then the value."""
        m = re.match(r"(?:set|make|update)\s+(?:the\s+)?(?P<rest>.+)$", t, re.I)
        if not m:
            return None
        f, rest = self.where(m.group("rest"))
        cv = self.split_column_value(rest)
        if not cv:
            return None
        return Plan(ops=[SetValue(column=cv[0], value=as_value(cv[1]), where=f)])

    def _clear(self, t: str) -> Plan | None:
        m = re.match(r"(?:clear|blank(?:\s+out)?|empty|null(?:\s+out)?|remove|delete|erase|strip)\s+(?:the\s+)?(?:values?\s+|text\s+|word\s+|symbol\s+)?"
                     r"(?P<vals>.+?)\s+(?:in|from|out\s+of)\s+(?:the\s+)?(?:column\s+)?(?P<c>.+?)(?:\s+column)?$", t, re.I)
        if not m:
            return None
        col = self.column(m.group("c"))
        vals = split_values(m.group("vals"))
        whole = [v for v in vals if col in self.ctx.columns_with_value(v)]
        if len(whole) == len(vals):
            return Plan(ops=[ReplaceValues(mapping={v: None for v in vals}, columns=[col])])
        if len(vals) == 1:                                  # part of the text: '$', ' (est)'
            return Plan(ops=[ReplaceText(column=col, find=vals[0], replace="")])
        missing = [v for v in vals if v not in whole]
        raise Ask(f"I could not find {', '.join(repr(v) for v in missing)} as a value in {col}.")

    def _mapping_plan(self, sources: list[str], target: str | None, col_spec: str | None, everywhere: bool) -> Plan:
        if col_spec:
            col = self.column(col_spec)
            whole = [s for s in sources if col in self.ctx.columns_with_value(s)]
            if not whole and len(sources) == 1 and self.ctx.column_values(col) and any(
                    sources[0].casefold() in v.casefold() for v in self.ctx.column_values(col)):
                return Plan(ops=[ReplaceText(column=col, find=sources[0], replace=target or "")])
            if not whole:
                raise Ask(f"I could not find {', '.join(repr(s) for s in sources)} in {col}.")
            return Plan(ops=[ReplaceValues(mapping={s: target for s in sources}, columns=[col])])
        cols = sorted({c for s in sources for c in self.ctx.columns_with_value(s)})
        if not cols:
            raise Ask(f"I could not find {', '.join(repr(s) for s in sources)} as a value in any column. "
                      "Check the spelling, or name the column ('… in City').")
        if len(cols) > 1 and not everywhere:
            raise Ask(f"{', '.join(repr(s) for s in sources)} appears in {', '.join(cols)}. Which column should I change? "
                      "(Or say 'everywhere'.)")
        return Plan(ops=[ReplaceValues(mapping={s: target for s in sources}, columns=cols)])

    _TAIL = r"(?:\s+(?:in|for|on|within)\s+(?:the\s+)?(?:column\s+)?(?P<col>[^,]+?)(?:\s+column)?)?(?P<every>\s+everywhere|\s+in\s+(?:all|every)\s+(?:columns?|tables?|files?|sheets?))?$"

    def _replace(self, t: str) -> Plan | None:
        m = re.match(r"(?:replace|change|map|convert|correct|fix|standardi[sz]e|turn|rename|bucket|group|put|categori[sz]e|merge)\s+"
                     r"(?:all\s+|the\s+|every\s+)?(?:values?\s+|cells?\s+|spellings?\s+)?(?P<a>.+?)\s+(?:to|with|into|as|under|->|=>|=)\s+(?P<b>.+?)" + self._TAIL, t, re.I)
        if not m:
            return None
        return self._mapping_plan(split_values(m.group("a")), as_value(m.group("b")), m.group("col"), bool(m.group("every")))

    def _statement(self, t: str) -> Plan | None:
        m = re.match(r"(?P<a>.+?)\s+(?:is|are|should\s+be|must\s+be|means|mean|=|->|=>|becomes|become)\s+(?:actually\s+|really\s+|the\s+same\s+as\s+)?"
                     r"(?P<b>.+?)" + self._TAIL, t, re.I)
        if not m:
            return None
        a = m.group("a")
        ma = re.match(r"(?P<vals>.+?)\s+in\s+(?:the\s+)?(?:column\s+)?(?P<col>.+)$", a, re.I)   # 'TBD and TBC in Status mean empty'
        col_spec = m.group("col")
        if ma and self.ctx.find_column(ma.group("col")) and not col_spec:
            a, col_spec = ma.group("vals"), ma.group("col")
        sources = split_values(a)
        if not col_spec and not all(self.ctx.columns_with_value(s) for s in sources):
            return None                                      # not a statement about values in the data
        if col_spec and not self.ctx.find_column(col_spec):
            return None
        return self._mapping_plan(sources, as_value(m.group("b")), col_spec, bool(m.group("every")))
