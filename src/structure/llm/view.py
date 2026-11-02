"""The view of a region shown to the local model (spec 10.3, 10.4). Built in memory, never stored."""
from __future__ import annotations

from collections import Counter

from ..header import View, find_group_rows, score_candidates
from ..model import col_letter, range_a1

PROMPT_VERSION = "structure-choice/2"
MAX_PROMPT_TOKENS = 12_000
LEGEND = ("legend: L label · T long text · N number · D date · P period · B boolean · E error · "
          "F formula without result · . empty · ~ merged continuation")
QUESTIONS = {
    "header_row": "which row holds the column names?",
    "region_class": "what kind of region is this?",
    "soft_cut": "is the marked empty row or column a boundary between two separate tables?",
    "footer": "is the marked row a footer (total or note) or a data row?",
}
# View sizes in shrinking steps: (first rows, value width, max columns) - spec 10.4 "Budget".
_STEPS = [(30, 32, 30), (20, 32, 30), (12, 32, 30), (12, 16, 30), (12, 16, 16)]


def _class_line(view: View, r: int, cols: list[int]) -> str:
    out = []
    for c in cols:
        cell = view.rows[r].get(c)
        if cell is not None:
            out.append(cell.cls)
        elif any(m[0] <= r <= m[2] and m[1] <= c <= m[3] and (m[0], m[1]) != (r, c) for m in view.sheet.merges):
            out.append("~")
        else:
            out.append(".")
    return " ".join(out)


def _header_like_rows(view: View, params: dict) -> set:
    """Rows scoring at least header_min_score, plus their group rows (labels_only level)."""
    rows = {r for r, s in score_candidates(view, params) if s >= params["header_min_score"]}
    for r in list(rows):
        rows.update(find_group_rows(view, r))
    return rows


def _values(view: View, r: int, cols: list[int], width: int, only_labels: bool) -> str:
    parts = []
    for c in cols:
        cell = view.rows[r].get(c)
        text = "" if cell is None or cell.text is None else cell.text
        if only_labels and cell is not None and cell.cls not in ("L", "P"):
            text = ""
        parts.append(text.replace("\n", " ")[:width])
    return " | ".join(parts)


def _shown_rows(view: View, first: int, focus_row, cut) -> list[int]:
    rows = view.row_list
    keep = set(rows[:first]) | set(rows[-5:])
    if focus_row is not None:
        keep.add(focus_row)
    if cut:  # 5 rows on each side of the cut and the first 10 of the far side
        axis, pos = cut
        if axis == 0:
            near = [r for r in rows if r < pos]
            far = [r for r in rows if r >= pos]
            keep |= set(near[-5:]) | set(far[:10])
    return sorted(keep)


def build_view(sheet, coords, ctx: dict, privacy: str, params: dict, cut=None, focus_row=None):
    """Returns (region block of the prompt, class grid); shrinks the block until it fits the token budget."""
    view = View(sheet, coords)
    like = _header_like_rows(view, params) if privacy == "labels_only" else set()
    sheet_name = f"sheet_{sheet.index}" if privacy == "structure_only" else ctx.get("sheet_name", "")
    for first, width, max_cols in _STEPS:
        all_cols = list(range(view.c1, view.c2 + 1))
        cols = all_cols if len(all_cols) <= max_cols else all_cols[:max_cols - 6] + all_cols[-6:]
        if cut and cut[0] == 1:  # a column cut always shows the 5 columns on each side
            near = [c for c in all_cols if c < cut[1]][-5:]
            far = [c for c in all_cols if c >= cut[1]][:5]
            cols = sorted(set(cols) | set(near) | set(far))
        lines = [f"REGION {range_a1(view.r1, view.c1, view.r2, view.c2)} · {len(view.row_list)} rows × "
                 f"{view.eff_width} columns · SHEET \"{sheet_name}\""
                 + (f" · frozen rows 1–{sheet.freeze_row}" if sheet.freeze_row else "")]
        if privacy == "full":
            lines.append(f"FILE {ctx.get('path', '')}")
            above = sorted({r for (r, c) in sheet.cells if r < view.r1 and view.c1 <= c <= view.c2})[-3:]
            for r in above:
                text = " | ".join((sheet.cells[(r, c)].text or "")[:width] for c in range(view.c1, view.c2 + 1)
                                  if (r, c) in sheet.cells)
                lines.append(f"ABOVE THE REGION   row {r}: {text}")
        if len(all_cols) > len(cols):
            lines.append(f"{len(all_cols) - len(cols)} of {len(all_cols)} columns not shown")
        lines.append(LEGEND)
        grid: list[str] = []
        shown = _shown_rows(view, first, focus_row, cut)
        prev = view.r1 - 1
        for r in shown:
            gap = r - prev - 1
            if gap > 0:
                between = [x for x in view.row_list if prev < x < r]
                if between:
                    pat = Counter(_class_line(view, x, cols) for x in between).most_common(1)[0][0]
                    lines.append(f"rows {prev + 1}–{r - 1}: {len(between)} rows, mostly the pattern `{pat}`")
                else:
                    lines.append(f"(rows {prev + 1}–{r - 1} empty)")
            text = ""
            if privacy == "full":
                text = "  | " + _values(view, r, cols, width, False)
            elif privacy == "labels_only" and r in like:
                text = "  | " + _values(view, r, cols, width, True)
            mark = "  <== marked row" if r == focus_row else ""
            hidden = " (hidden)" if r in sheet.hidden_rows else ""
            grid.append(_class_line(view, r, cols))
            lines.append(f"row {r:<4}{grid[-1]}{text}{hidden}{mark}")
            prev = r
        text = "\n".join(lines)
        if len(text) // 3 <= MAX_PROMPT_TOKENS:
            break
    if cut:
        axis, pos = cut
        text += f"\nCANDIDATE CUT: the empty {'row' if axis == 0 else 'column'} at " \
                f"{pos if axis == 0 else col_letter(pos)}"
    return text, grid


def build_prompt(view_text: str, decisions: list[dict], examples_text: str) -> str:
    """One question per decision; options are always the rules' option ids (spec 10.5)."""
    parts = [f"TASK {PROMPT_VERSION}. Decide how this spreadsheet region is laid out.", examples_text, view_text, "QUESTION"]
    for d in decisions:
        parts.append(f"{d['id']} {d['kind']}: {QUESTIONS[d['kind']]}\n   options: " + " · ".join(d["options"]))
    parts.append("For each question give a short reason (at most 20 words), then your choice.")
    return "\n".join(p for p in parts if p)
