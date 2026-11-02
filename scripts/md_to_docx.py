"""Convert one of the repo's Markdown documents to a Word file:  python scripts/md_to_docx.py <in.md> <out.docx>

Handles what these docs use: headings, paragraphs, bullet and numbered lists, pipe tables, **bold**, `code`, horizontal rules.
"""
import re
import sys
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

INLINE = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`|(?<![\w*])\*[^*\s][^*]*\*(?![\w*]))")


def runs(par, text, size=None, bold=False):
    for part in INLINE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            runs(par, part[2:-2], size=size, bold=True)       # bold text may contain `code`
            continue
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            r = par.add_run(part[1:-1])
            r.italic = True
        elif part.startswith("`") and part.endswith("`"):
            r = par.add_run(part[1:-1])
            r.font.name = "Consolas"
            r.font.size = Pt((size or 10) - 1)
            r.font.color.rgb = RGBColor(0x9A, 0x1F, 0x4F)
        else:
            r = par.add_run(part)
            r.bold = bold or None
        if size and not (part.startswith("`")):
            r.font.size = Pt(size)


def shade(cell, hex_):
    pr = cell._tc.get_or_add_tcPr()
    sh = OxmlElement("w:shd")
    sh.set(qn("w:val"), "clear")
    sh.set(qn("w:color"), "auto")
    sh.set(qn("w:fill"), hex_)
    pr.append(sh)


def table(doc, rows):
    header, body = rows[0], rows[1:]
    t = doc.add_table(rows=1, cols=len(header))
    t.style = "Table Grid"
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    t.autofit = False
    weight = [min(max(len(re.sub(r"[*`]", "", r[i])) if i < len(r) else 0 for r in rows), 60) + 9 for i in range(len(header))]
    total_w = 7.0 * 914400                                           # usable width in EMU (letter, 0.75 in margins)
    for i, h in enumerate(header):
        c = t.rows[0].cells[i]
        c.text = ""
        runs(c.paragraphs[0], h, size=9, bold=True)
        shade(c, "D9E2F3")
    for r in body:
        cells = t.add_row().cells
        for i in range(len(header)):
            cells[i].text = ""
            runs(cells[i].paragraphs[0], r[i] if i < len(r) else "", size=9)
    for i, col in enumerate(t.columns):
        col.width = int(total_w * weight[i] / sum(weight))              # the table grid (LibreOffice reads this)
    for row in t.rows:
        for i, c in enumerate(row.cells):
            c.width = int(total_w * weight[i] / sum(weight))
    doc.add_paragraph()


def convert(src: Path, dst: Path) -> None:
    doc = Document()
    doc.styles["Normal"].font.name = "Calibri"
    doc.styles["Normal"].font.size = Pt(10.5)
    for s in doc.sections:
        s.left_margin = s.right_margin = Pt(54)
    lines = src.read_text(encoding="utf8").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("|"):
            block = []
            while i < len(lines) and lines[i].startswith("|"):
                cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
                if not all(re.fullmatch(r":?-{2,}:?", c) for c in cells):
                    block.append(cells)
                i += 1
            table(doc, block)
            continue
        m = re.match(r"^(#{1,4}) (.*)", line)
        if m:
            level = min(len(m.group(1)), 3) - (0 if len(m.group(1)) > 1 else 0)
            doc.add_heading(re.sub(r"[*`]", "", m.group(2)), level=0 if len(m.group(1)) == 1 else level)
        elif re.fullmatch(r"-{3,}", line.strip()):
            pass
        elif re.match(r"^\s*[*-] ", line):
            p = doc.add_paragraph(style="List Bullet")
            runs(p, re.sub(r"^\s*[*-] ", "", line))
        elif re.match(r"^\s*\d+\. ", line):
            p = doc.add_paragraph(style="List Number")
            runs(p, re.sub(r"^\s*\d+\. ", "", line))
        elif line.strip():
            text = line.strip()
            while i + 1 < len(lines) and lines[i + 1].strip() and not re.match(r"^(#|\||\s*[*-] |\s*\d+\. |-{3,})", lines[i + 1]):
                i += 1
                text += " " + lines[i].strip()
            p = doc.add_paragraph()
            runs(p, text)
        i += 1
    doc.save(dst)


if __name__ == "__main__":
    convert(Path(sys.argv[1]), Path(sys.argv[2]))
    print("wrote", sys.argv[2])
