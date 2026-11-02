"""Streaming OOXML workbook reader (spec 4.2): one reader for facts and values, in A1 coordinates.

Cells are read with `lxml.iterparse`, each element cleared once processed, so a sheet never exists as a
DOM. Small parts (styles, tables, drawings, comments, pivots) are parsed whole. The parser is hardened:
no DTDs, no entity expansion, no network access.
"""
from __future__ import annotations

import posixpath
import re
import unicodedata
import zipfile
from datetime import datetime

from lxml import etree

from . import colors
from .classify import mark_period_years, number_cell, text_cell
from .errors import FileUnreadable
from .model import Cell, Sheet, Workbook, parse_ref

M = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
XDR = "{http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing}"

MAX_SHEETS = 1_000
MAX_SHEET_CELLS = 50_000_000
MAX_PACKAGE_BYTES = 5 << 30
MAX_PART_RATIO = 100
PROGRESS_ROWS = 20_000
EMU_PER_PX = 9525
_A1 = re.compile(r"^([A-Z]+)(\d+)$")
_EXTERNAL_REF = re.compile(r"\[\d+\]|\[[^\]]+\.[Xx][Ll][^\]]*\]")

# Built-in number formats that matter for typing cells (the rest render as plain numbers).
_BUILTIN_FMT = {
    0: "General", 1: "0", 2: "0.00", 3: "#,##0", 4: "#,##0.00", 9: "0%", 10: "0.00%", 11: "0.00E+00",
    14: "mm-dd-yy", 15: "d-mmm-yy", 16: "d-mmm", 17: "mmm-yy", 18: "h:mm AM/PM", 19: "h:mm:ss AM/PM",
    20: "h:mm", 21: "h:mm:ss", 22: "m/d/yy h:mm", 45: "mm:ss", 46: "[h]:mm:ss", 47: "mmss.0", 49: "@",
    **{i: "mm-dd-yy" for i in (27, 28, 29, 30, 31, 32, 33, 34, 35, 36, 50, 51, 52, 53, 54, 55, 56, 57, 58)},
}


def col_number(letters: str) -> int:
    n = 0
    for ch in letters:
        n = n * 26 + ord(ch) - 64
    return n


def sheet_key(name: str, index: int, used: set) -> str:
    """S-24: NFKC, lower-case, other runs -> `_`, cut to 40; a collision appends the index."""
    k = re.sub(r"[^a-z0-9]+", "_", unicodedata.normalize("NFKC", name).lower()).strip("_")[:40] or f"sheet_{index}"
    if k in used:
        k = f"{k}_{index}"
    used.add(k)
    return k


class Package:
    """An open XLSX package with hardened XML access and relationship lookup."""

    def __init__(self, path: str):
        try:
            self.zf = zipfile.ZipFile(path)
        except zipfile.BadZipFile as e:
            raise FileUnreadable("corrupt package") from e
        infos = self.zf.infolist()
        if sum(i.file_size for i in infos) > MAX_PACKAGE_BYTES:
            raise FileUnreadable("uncompressed package larger than 5 GB")
        for i in infos:
            if i.file_size > (1 << 20) and i.file_size / max(i.compress_size, 1) > MAX_PART_RATIO:
                raise FileUnreadable("package part exceeds the compression-ratio limit")
        self.names = set(self.zf.namelist())

    def _check(self, name: str) -> None:
        if name not in self.names:
            raise FileUnreadable(f"missing package part {name}")
        with self.zf.open(name) as f:
            if b"<!DOCTYPE" in f.read(2048):
                raise FileUnreadable("DTDs are not allowed")

    def parse(self, name: str):
        """Whole part as an element tree root (small parts only)."""
        self._check(name)
        parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, huge_tree=False)
        try:
            return etree.fromstring(self.zf.read(name), parser)
        except etree.XMLSyntaxError as e:
            raise FileUnreadable(f"corrupt XML part {name}") from e

    def stream(self, name: str, tags: tuple):
        """Streaming `end` events for `tags` (Clark names) with hardened options."""
        self._check(name)
        try:
            yield from etree.iterparse(self.zf.open(name), events=("end",), tag=tags, resolve_entities=False,
                                       no_network=True, load_dtd=False, huge_tree=False)
        except etree.XMLSyntaxError as e:
            raise FileUnreadable(f"corrupt XML part {name}") from e

    def rels(self, part: str) -> dict:
        """rId -> (type suffix, absolute part path) of a part's relationships."""
        rel_name = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
        if rel_name not in self.names:
            return {}
        out = {}
        for rel in self.parse(rel_name):
            target = rel.get("Target", "")
            if rel.get("TargetMode") == "External":
                path = None
            elif target.startswith("/"):
                path = target[1:]
            else:
                path = posixpath.normpath(posixpath.join(posixpath.dirname(part), target))
            out[rel.get("Id")] = (rel.get("Type", "").rsplit("/", 1)[-1], path)
        return out


# ------------------------------------------------------------------ styles


class Styles:
    """cellXfs -> number format, and which xfs hide their value (S-18); dxfs for conditional formats."""

    def __init__(self, pkg: Package, theme: list[str]):
        self.theme = theme
        self.numfmts: dict[int, str] = {}
        self.xfs: list[tuple[int, int, int]] = []
        self.fonts: list = []
        self.fills: list = []
        self.dxfs: list[dict] = []
        if "xl/styles.xml" not in pkg.names:
            return
        root = pkg.parse("xl/styles.xml")
        for nf in root.iterfind(f"{M}numFmts/{M}numFmt"):
            self.numfmts[int(nf.get("numFmtId"))] = nf.get("formatCode")
        for font in root.iterfind(f"{M}fonts/{M}font"):
            self.fonts.append(colors.Color.from_xml(font.find(f"{M}color")))
        for fill in root.iterfind(f"{M}fills/{M}fill"):
            pat = fill.find(f"{M}patternFill")
            solid = pat is not None and pat.get("patternType") == "solid"
            self.fills.append(colors.Color.from_xml(pat.find(f"{M}fgColor")) if solid else None)
        for xf in root.iterfind(f"{M}cellXfs/{M}xf"):
            self.xfs.append((int(xf.get("numFmtId", 0)), int(xf.get("fontId", 0)), int(xf.get("fillId", 0))))
        for dxf in root.iterfind(f"{M}dxfs/{M}dxf"):
            nf = dxf.find(f"{M}numFmt")
            fill = dxf.find(f"{M}fill/{M}patternFill")
            self.dxfs.append({
                "font": colors.Color.from_xml(dxf.find(f"{M}font/{M}color")),
                "fill": colors.Color.from_xml(fill.find(f"{M}bgColor") if fill is not None and fill.find(f"{M}bgColor") is not None
                                              else (fill.find(f"{M}fgColor") if fill is not None else None)),
                "numfmt": nf.get("formatCode") if nf is not None else None})
        self.hiding = {i for i in range(len(self.xfs)) if self._hides(i)}

    def fmt(self, xf: int) -> str:
        num_id = self.xfs[xf][0] if xf < len(self.xfs) else 0
        return self.numfmts.get(num_id) or _BUILTIN_FMT.get(num_id, "General")

    def colors_of(self, xf: int) -> tuple[str, str]:
        """(font RGB, background RGB) of a style; Excel's defaults are black on white."""
        _, font_id, fill_id = self.xfs[xf] if xf < len(self.xfs) else (0, 0, 0)
        font = colors.resolve(self.fonts[font_id] if font_id < len(self.fonts) else None, self.theme) or "000000"
        fill = colors.resolve(self.fills[fill_id] if fill_id < len(self.fills) else None, self.theme) or "FFFFFF"
        return font, fill

    def _hides(self, xf: int) -> bool:
        """A style hides its value when font and fill contrast < 1.1:1 or the format is `;;;`."""
        if self.fmt(xf) == ";;;":
            return True
        font, fill = self.colors_of(xf)
        return colors.contrast(font, fill) < 1.1

    def dxf_hides(self, dxf_id: int, top_left_xf: int) -> bool:
        if dxf_id >= len(self.dxfs):
            return False
        d = self.dxfs[dxf_id]
        if d["numfmt"] == ";;;":
            return True
        base_font, base_fill = self.colors_of(top_left_xf)
        font = colors.resolve(d["font"], self.theme) or base_font
        bg = colors.resolve(d["fill"], self.theme) or base_fill
        return colors.contrast(font, bg) < 1.1


# ------------------------------------------------------------------ workbook


def read_xlsx(path: str, macro: bool = False, on_rows=None) -> Workbook:
    """The workbook's facts and cells. `on_rows(sheet name, rows read, last row or None)` is called every
    PROGRESS_ROWS rows of a sheet, so a long read can report progress (and be cancelled: it may raise)."""
    pkg = Package(path)
    book = pkg.parse("xl/workbook.xml")
    rels = pkg.rels("xl/workbook.xml")
    theme = colors.parse_theme(pkg.zf.read("xl/theme/theme1.xml") if "xl/theme/theme1.xml" in pkg.names else None)
    styles = Styles(pkg, theme)
    shared = _shared_strings(pkg, rels)
    pr = book.find(f"{M}workbookPr")
    date1904 = pr is not None and pr.get("date1904") in ("1", "true")
    calc, prot = book.find(f"{M}calcPr"), book.find(f"{M}workbookProtection")
    wb = Workbook([], macro_enabled=macro,
                  external_links=sum(1 for t, _ in rels.values() if t == "externalLink"),
                  calc_on_load=calc is not None and calc.get("fullCalcOnLoad") in ("1", "true"),
                  structure_locked=prot is not None and prot.get("lockStructure") in ("1", "true"))
    used_keys: set = set()
    entries = []
    for idx, s in enumerate(book.iterfind(f"{M}sheets/{M}sheet")):
        typ, part = rels.get(s.get(f"{R}id"), (None, None))
        sh = Sheet(idx, s.get("name"), sheet_key(s.get("name"), idx, used_keys), state=s.get("state") or "visible")
        entries.append((sh, typ, part))
    for name in book.iterfind(f"{M}definedNames/{M}definedName"):
        local = name.get("localSheetId")
        wb.names.append({"name": name.get("name"), "formula": (name.text or "").strip(),
                         "sheet_index": int(local) if local is not None else None})
    for n, (sh, typ, part) in enumerate(entries):
        if typ != "worksheet" or part is None:
            continue                      # chartsheet: its index is consumed, nothing to read
        wb.sheets.append(sh)
        if n >= MAX_SHEETS:
            sh.error = "sheet_too_large"
            continue
        try:
            _read_sheet(pkg, sh, part, shared, styles, date1904, on_rows)
        except FileUnreadable:
            raise
        except Exception as e:  # noqa: BLE001 - one bad sheet must not kill the file (K-14)
            sh.error = type(e).__name__      # cells read so far are kept: they become `unread` placements
    for name in wb.names:
        if name["name"] == "_xlnm.Print_Area" and name["sheet_index"] is not None:
            sh = next((s for s in wb.sheets if s.index == name["sheet_index"]), None)
            if sh is not None:
                try:
                    sh.print_area = parse_ref(name["formula"].split("!")[-1].split(",")[0])
                except ValueError:
                    pass
    return wb


def _shared_strings(pkg: Package, rels: dict) -> list[str]:
    part = next((p for t, p in rels.values() if t == "sharedStrings"), None)
    if part is None or part not in pkg.names:
        return []
    out = []
    # Rich-text runs are joined; phonetic runs (`rPh`) are not part of the text.
    for _ev, si in pkg.stream(part, (f"{M}si",)):
        out.append("".join(t.text or "" for t in si.iter(f"{M}t") if t.getparent().tag != f"{M}rPh"))
        si.clear()
    return out


# ------------------------------------------------------------------ worksheet


def _make_cell(el, shared: list[str], styles: Styles, date1904: bool, formula_text: str | None) -> Cell | None:
    t, style = el.get("t"), int(el.get("s") or 0)
    fmt = styles.fmt(style)
    v = el.find(f"{M}v")
    vtext = v.text if v is not None else None
    if formula_text is not None:
        if vtext in (None, ""):
            return Cell("F", None, "formula_uncached", fmt, formula=True, uncached=True, style=style)
    if t == "s":
        cell = text_cell(shared[int(vtext)], fmt, style) if vtext is not None else None
    elif t == "inlineStr":
        cell = text_cell("".join(x.text or "" for x in el.iterfind(f"{M}is//{M}t")), fmt, style)
    elif t == "str":
        cell = text_cell(vtext or "", fmt, style)
    elif t == "b":
        cell = Cell("B", "TRUE" if vtext == "1" else "FALSE", "b", fmt, style=style)
    elif t == "e":
        cell = Cell("E", vtext, "e", fmt, style=style, error=True) if vtext else None
    elif t == "d":
        cell = _iso_date_cell(vtext, fmt, style)
    else:
        cell = number_cell(float(vtext), fmt, date1904, style) if vtext not in (None, "") else None
    if cell is None and formula_text is not None:
        cell = Cell("L", None, "s", fmt, style=style)      # formula whose cached result is an empty string
    if cell is not None and formula_text is not None:
        cell.formula = True
    return cell


def _iso_date_cell(text: str | None, fmt: str | None, style: int) -> Cell | None:
    if not text:
        return None
    try:
        d = datetime.fromisoformat(text.replace("Z", ""))
    except ValueError:
        return text_cell(text, fmt, style)
    if d.time().isoformat() == "00:00:00":
        return Cell("D", d.date().isoformat(), "date", fmt, style=style)
    return Cell("D", d.isoformat(timespec="seconds"), "datetime", fmt, style=style)


def _read_sheet(pkg: Package, sh: Sheet, part: str, shared: list[str], styles: Styles, date1904: bool,
                on_rows=None) -> None:
    col_ranges: list[tuple[int, int, bool, int, float | None]] = []
    row_heights: dict[int, float] = {}
    cf_raw, default_col_w, default_row_h = [], 8.43, 15.0
    shared_formulas: dict[str, str] = {}
    last_col: dict = {}                           # id(row element) -> last column, for cells without `r`
    tags = tuple(M + t for t in ("c", "row", "col", "mergeCell", "pane", "autoFilter", "sheetProtection",
                                 "conditionalFormatting", "dataValidation", "dimension", "sheetFormatPr"))
    for _ev, el in pkg.stream(part, tags):
        tag = el.tag[len(M):]
        if tag == "c":
            _read_cell_element(sh, el, shared, styles, date1904, shared_formulas, last_col)
            el.clear()
        elif tag == "row":
            r = int(el.get("r") or 0)
            if on_rows and r % PROGRESS_ROWS == 0:
                on_rows(sh.name, r, parse_ref(sh.declared_dim)[2] if sh.declared_dim else None)
            if el.get("hidden") in ("1", "true"):
                sh.hidden_rows.add(r)
            if el.get("outlineLevel"):
                sh.row_outline[r] = int(el.get("outlineLevel"))
            if el.get("ht"):
                row_heights[r] = float(el.get("ht"))
            el.clear()
            while el.getprevious() is not None:
                del el.getparent()[0]
        elif tag == "col":
            col_ranges.append((int(el.get("min")), int(el.get("max")), el.get("hidden") in ("1", "true"),
                               int(el.get("outlineLevel") or 0), float(el.get("width")) if el.get("width") else None))
        elif tag == "mergeCell":
            sh.merges.append(parse_ref(el.get("ref")))
        elif tag == "pane" and el.get("state") in ("frozen", "frozenSplit"):
            sh.freeze_row, sh.freeze_col = int(float(el.get("ySplit") or 0)), int(float(el.get("xSplit") or 0))
        elif tag == "autoFilter":
            sh.autofilter = el.get("ref")
            sh.filter_criteria = el.find(f"{M}filterColumn") is not None
        elif tag == "sheetProtection" and el.get("sheet") in ("1", "true"):
            sh.protected, sh.protected_password = True, bool(el.get("password") or el.get("hashValue"))
        elif tag == "conditionalFormatting":
            cf_raw.append(_cf_element(el))
        elif tag == "dataValidation":
            sh.validations.append(_dv_element(el))
        elif tag == "dimension":
            sh.declared_dim = el.get("ref")
        elif tag == "sheetFormatPr":
            default_col_w = float(el.get("defaultColWidth") or default_col_w)
            default_row_h = float(el.get("defaultRowHeight") or default_row_h)
    if len(sh.cells) > MAX_SHEET_CELLS:
        sh.cells.clear()
        sh.error = "sheet_too_large"
        return
    mark_period_years(sh.cells)
    used = sh.used_range()
    for lo, hi, hidden, level, _w in col_ranges:      # never expand a max="16384" range (K-17)
        for c in range(lo, min(hi, used[3] if used else hi) + 1):
            if hidden:
                sh.hidden_cols.add(c)
            if level:
                sh.col_outline[c] = level
    for ranges, rules in cf_raw:
        top_left = sh.cells.get((ranges[0][0], ranges[0][1]))
        for typ, dxf_id, formula in rules:
            if dxf_id is None:
                continue                  # colour scales, data bars and icon sets cannot hide a value (K-12)
            sh.cond_formats.append({
                "type": typ, "ranges": ranges, "always": typ == "expression" and formula.strip().upper() in ("TRUE", "1"),
                "hides": styles.dxf_hides(dxf_id, top_left.style if top_left else 0)})
    rels = pkg.rels(part)
    widths = {}
    for lo, hi, _h, _l, w in col_ranges:
        if w is not None:
            for c in range(lo, min(hi, lo + 200) + 1):
                widths[c] = w
    for typ, target in rels.values():
        if target is None or target not in pkg.names:
            continue
        if typ == "table":
            sh.excel_tables.append(_read_table(pkg.parse(target)))
        elif typ == "pivotTable":
            piv = _read_pivot(pkg, target)
            if piv:
                sh.pivots.append(piv)
        elif typ == "drawing":
            sh.drawings += _read_drawing(pkg.parse(target), widths, default_col_w, row_heights, default_row_h)
        elif typ in ("comments", "threadedComment"):
            _read_comments(pkg.parse(target), sh)


def _read_cell_element(sh: Sheet, el, shared, styles, date1904, shared_formulas: dict, last_col: dict) -> None:
    parent = el.getparent()
    ref = el.get("r")
    if ref:
        m = _A1.match(ref)
        row, col = int(m.group(2)), col_number(m.group(1))
    else:                                     # some writers omit `r`: the position in the row decides
        row = int(parent.get("r") or 0)
        col = last_col.get(id(parent), 0) + 1
    last_col[id(parent)] = col
    f = el.find(f"{M}f")
    ftext = None
    if f is not None:
        ftext = f.text or ""
        if f.get("t") == "shared":
            if f.text:
                shared_formulas[f.get("si")] = f.text
            else:
                ftext = shared_formulas.get(f.get("si"), "")
    cell = _make_cell(el, shared, styles, date1904, ftext)
    if cell is None:
        return
    sh.cells[(row, col)] = cell
    if ftext is not None:
        flags = set()
        if _EXTERNAL_REF.search(ftext):
            flags.add("external")
        if "#REF!" in ftext:
            flags.add("broken")
        if flags:
            sh.formula_flags[(row, col)] = flags
        if flags or cell.uncached:
            cell.formula_text = ("=" + ftext)[:300]
    if cell.style in styles.hiding:
        sh.hidden_style_cells.add((row, col))


def _cf_element(el) -> tuple[list, list]:
    ranges = [parse_ref(x) for x in el.get("sqref", "").split()]
    rules = []
    for rule in el.iterfind(f"{M}cfRule"):
        dxf = rule.get("dxfId")
        rules.append((rule.get("type"), int(dxf) if dxf is not None else None,
                      (rule.findtext(f"{M}formula") or "")))
    return ranges, rules


def _dv_element(el) -> dict:
    return {"ranges": [parse_ref(x) for x in el.get("sqref", "").split()], "type": el.get("type") or "none",
            "operator": el.get("operator") or "between", "f1": el.findtext(f"{M}formula1"),
            "f2": el.findtext(f"{M}formula2"), "allow_blank": el.get("allowBlank") in ("1", "true")}


def _read_table(root) -> dict:
    hdr = root.get("headerRowCount")
    af = root.find(f"{M}autoFilter")
    return {"name": root.get("name"), "ref": root.get("ref"), "header_rows": 1 if hdr is None else int(hdr),
            "totals_rows": int(root.get("totalsRowCount") or 0),
            "columns": [c.get("name") for c in root.iterfind(f"{M}tableColumns/{M}tableColumn")],
            "criteria": af is not None and af.find(f"{M}filterColumn") is not None}


def _read_pivot(pkg: Package, part: str) -> dict | None:
    root = pkg.parse(part)
    loc = root.find(f"{M}location")
    if loc is None or not loc.get("ref"):
        return None
    out = {"ref": loc.get("ref"), "first_header_row": int(loc.get("firstHeaderRow") or 0),
           "first_data_row": int(loc.get("firstDataRow") or 1), "cache_sheet": None,
           "cache_ref": None, "refresh_on_load": False, "refreshed": ""}
    cache_part = next((p for t, p in pkg.rels(part).values() if t == "pivotCacheDefinition"), None)
    if cache_part and cache_part in pkg.names:
        cache = pkg.parse(cache_part)
        src = cache.find(f"{M}cacheSource/{M}worksheetSource")
        out.update(refresh_on_load=cache.get("refreshOnLoad") in ("1", "true"), refreshed=cache.get("refreshedDate") or "",
                   cache_sheet=src.get("sheet") if src is not None else None,
                   cache_ref=src.get("ref") if src is not None else None)
    return out


def _read_comments(root, sh: Sheet) -> None:
    """Comment text is a data artifact: it goes to cells.parquet, never to a message (S-28)."""
    for c in root.iter(f"{M}comment"):
        sh.comments.setdefault(_ref_cell(c.get("ref")), "".join(t.text or "" for t in c.iter(f"{M}t")))
    for c in root.iter("{http://schemas.microsoft.com/office/spreadsheetml/2018/threadedcomments}threadedComment"):
        text = c.findtext("{http://schemas.microsoft.com/office/spreadsheetml/2018/threadedcomments}text") or ""
        sh.comments.setdefault(_ref_cell(c.get("ref")), text)


def _ref_cell(ref: str) -> tuple[int, int]:
    m = _A1.match(ref)
    return int(m.group(2)), col_number(m.group(1))


def _read_drawing(root, widths: dict, default_w: float, heights: dict, default_h: float) -> list[dict]:
    """Covered range of each anchor; one-cell and absolute anchors are converted with widths and heights."""
    def col_px(c: int) -> float:
        return (widths.get(c, default_w)) * 7 + 5

    def row_px(r: int) -> float:
        return heights.get(r, default_h) * 96 / 72

    def span(start: int, size_px: float, px) -> int:
        acc, n = 0.0, start
        while acc < size_px and n < 1_048_576:
            acc += px(n)
            n += 1
        return n - 1

    out = []
    for anchor in root:
        kind = anchor.tag.rsplit("}", 1)[-1]
        if kind not in ("twoCellAnchor", "oneCellAnchor", "absoluteAnchor"):
            continue
        name = next((e.get("name") for e in anchor.iter(f"{XDR}cNvPr")), "Object")
        frm, to, ext = anchor.find(f"{XDR}from"), anchor.find(f"{XDR}to"), anchor.find(f"{XDR}ext")
        if kind == "absoluteAnchor":
            pos = anchor.find(f"{XDR}pos")
            x, y = int(pos.get("x")) / EMU_PER_PX, int(pos.get("y")) / EMU_PER_PX
            c1 = r1 = 1
            while x > col_px(c1):
                x -= col_px(c1); c1 += 1
            while y > row_px(r1):
                y -= row_px(r1); r1 += 1
        else:
            c1, r1 = int(frm.findtext(f"{XDR}col")) + 1, int(frm.findtext(f"{XDR}row")) + 1
        if to is not None:
            c2, r2 = int(to.findtext(f"{XDR}col")) + 1, int(to.findtext(f"{XDR}row")) + 1
        elif ext is not None:
            c2 = span(c1, int(ext.get("cx")) / EMU_PER_PX, col_px)
            r2 = span(r1, int(ext.get("cy")) / EMU_PER_PX, row_px)
        else:
            c2, r2 = c1, r1
        out.append({"name": name, "box": (r1, c1, r2, c2)})
    return out
