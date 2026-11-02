"""City names: which values of a column name the same city (another name, a short form, a spelling mistake).

Design (see docs/city_name_matching_design.md). The work is done on the DISTINCT values of a column, never row by row, so a
table of millions of rows costs the same as one of a few thousand: a column holds a few hundred distinct cities at most.
Each distinct value goes through tiers that stop at the first answer:

    1. normalise (case, accents, punctuation, spaces)                      O(length)
    2. exact lookup in a hash of every known name                          O(1)
    3. close lookup: a delete-neighbourhood index (the SymSpell idea) finds every known name within 1 or 2 edits
       without comparing against the whole dictionary; each candidate is then verified with a real edit distance
    4. (the caller) values nobody knows are compared with each other, so a misspelling of a city that is in no list
       is still found

What the hash holds, in order of authority:
    * the project's own city list (`city_master`), when the project has one: these spellings win
    * config/city_names.yaml: cities with their other names, short forms and common misspellings (editable)
    * an optional gazetteer file (name, canonical, country) for wider coverage, filtered by country

The index is built once per process and cached by file time stamps, so only the first call pays for it.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SKIP = {"others", "other", "unspecified", "unknown", "all", "total", "na", "nan", "none", "india", "overall", "panindia",
        "national", "notapplicable", "tbd"}                    # not a city, and not worth a flag
_CACHE: dict[tuple, "CityIndex"] = {}


def normalize(value) -> str:
    """'New-Delhi ', 'NEW DELHI' and 'New Delhi' give 'newdelhi'; accents are dropped."""
    text = unicodedata.normalize("NFKD", str(value)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def max_edit(length: int, limit: int = 2) -> int:
    """How many edits a name of this length may be away from a known one: none for very short names, 1 up to five
    letters, `limit` for longer names (a short name 2 edits away is another word)."""
    return 0 if length < 4 else 1 if length <= 5 else limit


def edit_distance(a: str, b: str, cap: int) -> int:
    """Optimal-string-alignment distance (an insertion, deletion, substitution or swap of neighbours is one edit); returns
    cap + 1 as soon as the answer is above `cap`."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev2, prev = None, list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
            if i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        if min(cur) > cap:
            return cap + 1
        prev2, prev = prev, cur
    return prev[-1]


def _deletes(word: str, depth: int) -> set[str]:
    out, frontier = {word}, {word}
    for _ in range(depth):
        frontier = {w[:i] + w[i + 1:] for w in frontier for i in range(len(w))}
        out |= frontier
    return out


@dataclass(frozen=True)
class Match:
    canonical: str
    kind: str              # standard | alias | close
    distance: int = 0
    in_master: bool = True  # False: a known city that is not in the project's own list


class CityIndex:
    def __init__(self, groups: dict[str, set[str]], master: set[str], max_edits: int = 2):
        """groups: canonical spelling -> every name (any spelling) of that city; master: canonical spellings the project lists."""
        self.master_mode = bool(master)
        self.master = master
        self.max_edits = max_edits
        self.exact: dict[str, str] = {}
        clash: set[str] = set()
        for canonical, names in groups.items():
            for name in {canonical, *names}:
                n = normalize(name)
                if not n:
                    continue
                if n in self.exact and self.exact[n] != canonical:
                    clash.add(n)                                # one name for two cities is a guess: not used
                else:
                    self.exact[n] = canonical
        for n in clash:
            self.exact.pop(n, None)
        self.canonical_norm = {normalize(c): c for c in groups}
        self.deletes: dict[str, set[str]] = {}
        for n in self.exact:
            for d in _deletes(n, max_edit(len(n), max_edits)):
                self.deletes.setdefault(d, set()).add(n)

    def __len__(self) -> int:
        return len(self.exact)

    def resolve(self, value: str) -> Match | None:
        n = normalize(value)
        if not n or n in SKIP:
            return None
        if n in self.exact:
            canonical = self.exact[n]
            kind = "standard" if n == normalize(canonical) else "alias"
            return Match(canonical, kind, 0, canonical in self.master or not self.master_mode)
        cap = max_edit(len(n), self.max_edits)
        if not cap:
            return None
        found: dict[str, int] = {}
        for d in _deletes(n, cap):
            for key in self.deletes.get(d, ()):
                if key in found:
                    continue
                dist = edit_distance(n, key, min(cap, max_edit(len(key), self.max_edits)))
                if dist and dist <= min(cap, max_edit(len(key), self.max_edits)):
                    found[key] = dist
        if not found:
            return None
        best = min(found.values())
        winners = {self.exact[k] for k, dist in found.items() if dist == best}
        if len(winners) != 1:                                   # two different cities are equally close: do not guess
            return None
        canonical = winners.pop()
        return Match(canonical, "close", best, canonical in self.master or not self.master_mode)


def _read_yaml(path: Path) -> dict[str, list]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf8")) or {}
    return {str(k): [str(x) for x in (v or [])] for k, v in data.items()}


def _read_gazetteer(path: Path, countries: set[str]) -> dict[str, set[str]]:
    """Rows of `name` (and optionally `canonical`, `country`) from a csv / tsv; a row with no canonical names a city by itself."""
    import csv
    groups: dict[str, set[str]] = {}
    with path.open(encoding="utf8", newline="") as fh:
        reader = csv.DictReader(fh, delimiter="\t" if path.suffix.lower() in (".tsv", ".txt") else ",")
        for row in reader:
            if countries and (row.get("country") or "").strip().upper() not in countries:
                continue
            name = (row.get("name") or "").strip()
            if name:
                groups.setdefault((row.get("canonical") or name).strip(), set()).add(name)
    return groups


def _read_known(path: Path) -> list[str]:
    """One city per line; blank lines and `#` comments are ignored."""
    return [ln.strip() for ln in path.read_text(encoding="utf8").splitlines() if ln.strip() and not ln.lstrip().startswith("#")]


def build_index(names_file: str | None, master: list[str] | None = None, gazetteer: str | None = None,
                countries: list[str] | None = None, max_edits: int = 2, known_file: str | None = None) -> CityIndex:
    """Index of every name: the editable alias file, a gazetteer file, then the project's own list (whose spelling wins)."""
    def resolve(p):
        return None if not p else (Path(p) if Path(p).is_absolute() else REPO_ROOT / p)
    names_path, gaz_path, known_path = resolve(names_file), resolve(gazetteer), resolve(known_file)
    master = [m.strip() for m in (master or []) if str(m).strip()]
    key = (str(names_path), names_path.stat().st_mtime_ns if names_path and names_path.exists() else 0,
           str(gaz_path), gaz_path.stat().st_mtime_ns if gaz_path and gaz_path.exists() else 0,
           str(known_path), known_path.stat().st_mtime_ns if known_path and known_path.exists() else 0,
           tuple(sorted(c.upper() for c in countries or [])), tuple(master), max_edits)
    if key in _CACHE:
        return _CACHE[key]
    groups: dict[str, set[str]] = {c: set(v) for c, v in (_read_yaml(names_path).items() if names_path else [])}
    if gaz_path and gaz_path.exists():
        for c, v in _read_gazetteer(gaz_path, {x.upper() for x in countries or []}).items():
            groups.setdefault(c, set()).update(v)
    if known_path and known_path.exists():                      # the fallback list: only names nothing above already has
        seen = {normalize(n) for c, ns in groups.items() for n in {c, *ns}}
        for name in _read_known(known_path):
            if normalize(name) not in seen:
                groups.setdefault(name, set())
                seen.add(normalize(name))
    # the project's spelling wins: a group that has one of its names in the list is renamed to that spelling
    norm_to_group = {normalize(n): c for c, ns in groups.items() for n in {c, *ns}}
    master_set = set()
    for m in master:
        group = norm_to_group.get(normalize(m))
        names = groups.pop(group, set()) | {group} if group else set()
        groups.setdefault(m, set()).update(names)
        master_set.add(m)
    if len(_CACHE) > 8:
        _CACHE.clear()
    _CACHE[key] = CityIndex(groups, master_set, max_edits)
    return _CACHE[key]
