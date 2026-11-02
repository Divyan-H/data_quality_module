"""Colour resolution and contrast for S-18 (indexed, theme and tinted colours -> RGB, spec 4.2)."""
from __future__ import annotations

import colorsys
import re
from typing import NamedTuple

# Theme slots in the order Excel indexes them (0 = lt1, 1 = dk1, 2 = lt2, 3 = dk2, 4.. = accents).
_OFFICE_THEME = ["FFFFFF", "000000", "EEECE1", "1F497D", "4F81BD", "C0504D", "9BBB59", "8064A2",
                 "4BACC6", "F79646", "0000FF", "800080"]
_SLOTS = ["lt1", "dk1", "lt2", "dk2", "accent1", "accent2", "accent3", "accent4", "accent5",
          "accent6", "hlink", "folHlink"]


class Color(NamedTuple):
    """The attributes of a `<color>` / `<fgColor>` element in styles.xml."""
    rgb: str | None = None
    theme: int | None = None
    indexed: int | None = None
    tint: float = 0.0

    @classmethod
    def from_xml(cls, el) -> "Color | None":
        if el is None:
            return None
        return cls(el.get("rgb"), int(el.get("theme")) if el.get("theme") is not None else None,
                   int(el.get("indexed")) if el.get("indexed") is not None else None,
                   float(el.get("tint") or 0.0))


def parse_theme(xml: bytes | None) -> list[str]:
    """Theme colours from `xl/theme/theme1.xml`; the Office defaults when absent."""
    if not xml:
        return _OFFICE_THEME
    text = xml.decode("utf8", "replace")
    out = []
    for slot, default in zip(_SLOTS, _OFFICE_THEME):
        m = re.search(rf"<a:{slot}>\s*<a:(?:srgbClr val|sysClr[^>]*lastClr)=\"([0-9A-Fa-f]{{6}})\"", text)
        out.append(m.group(1).upper() if m else default)
    return out


def _tint(rgb: str, tint: float) -> str:
    r, g, b = (int(rgb[i:i + 2], 16) / 255 for i in (0, 2, 4))
    h, l, s = colorsys.rgb_to_hls(r, g, b)
    l = l * (1 + tint) if tint < 0 else l * (1 - tint) + tint
    r, g, b = colorsys.hls_to_rgb(h, max(0.0, min(1.0, l)), s)
    return "".join(f"{round(v * 255):02X}" for v in (r, g, b))


def resolve(color: Color | None, theme: list[str]) -> str | None:
    """Colour -> 'RRGGBB', or None when automatic / not set."""
    if color is None:
        return None
    if color.rgb:
        return color.rgb[-6:].upper()
    if color.theme is not None and color.theme < len(theme):
        return _tint(theme[color.theme], color.tint)
    if color.indexed is not None:
        from openpyxl.styles.colors import COLOR_INDEX
        if color.indexed < len(COLOR_INDEX):
            return COLOR_INDEX[color.indexed][-6:].upper()
    return None


def _lum(rgb: str) -> float:
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(int(rgb[i:i + 2], 16)) for i in (0, 2, 4))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)
