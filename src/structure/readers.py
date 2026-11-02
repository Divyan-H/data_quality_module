"""Readers for delimited text and Parquet, and the LibreOffice conversion of legacy formats.

Workbooks (XLSX/XLSM, and converted XLS/XLSB/ODS) are read by `xlsx.py`.
"""
from __future__ import annotations

import codecs
import csv
import io
import os
import shutil
import subprocess
import tempfile

from .classify import classify_text, mark_period_years
from .errors import FileUnreadable
from .model import Cell, Sheet, Workbook

MAX_FIELD = 1 << 20            # 1 MB per CSV field
MAX_FIELDS = 16_384            # fields per CSV record
CONVERT_TIMEOUT_S = 300

# The converter must never recalculate: stored formula results are kept exactly (decision 9).
_NO_RECALC = """<?xml version="1.0" encoding="UTF-8"?>
<oor:items xmlns:oor="http://openoffice.org/2001/registry" xmlns:xs="http://www.w3.org/2001/XMLSchema" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
<item oor:path="/org.openoffice.Office.Calc/Formula/Load"><prop oor:name="OOXMLRecalcMode" oor:op="fuse"><value>1</value></prop></item>
<item oor:path="/org.openoffice.Office.Calc/Formula/Load"><prop oor:name="ODFRecalcMode" oor:op="fuse"><value>1</value></prop></item>
<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>
</oor:items>"""


_LIBREOFFICE_PLACES = [os.path.expandvars(r"%LOCALAPPDATA%\Programs\LibreOffice\program\soffice.exe"),   # per-user (no admin)
                       r"C:\Program Files\LibreOffice\program\soffice.exe",
                       r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
                       "/Applications/LibreOffice.app/Contents/MacOS/soffice"]


def find_libreoffice() -> str | None:
    """The LibreOffice launcher: on the PATH, else where its installers put it (Windows does not add it to the PATH)."""
    return shutil.which("soffice") or shutil.which("libreoffice") or next((p for p in _LIBREOFFICE_PLACES if os.path.exists(p)), None)


def convert_to_xlsx(path: str, scratch: str) -> str:
    """XLS / XLSB / ODS -> XLSX with headless LibreOffice: fresh profile, no recalculation, killed on timeout."""
    exe = find_libreoffice()
    if not exe:
        raise FileUnreadable("format conversion needs LibreOffice, which is not installed")
    work = tempfile.mkdtemp(dir=scratch)
    profile = os.path.join(work, "profile")
    os.makedirs(os.path.join(profile, "user"))
    with open(os.path.join(profile, "user", "registrymodifications.xcu"), "w", encoding="utf8") as f:
        f.write(_NO_RECALC)
    proc = subprocess.Popen(
        [exe, "--headless", "--norestore", f"-env:UserInstallation=file:///{profile.replace(os.sep, '/')}",
         "--convert-to", "xlsx", "--outdir", work, path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        proc.wait(timeout=CONVERT_TIMEOUT_S)
    except subprocess.TimeoutExpired as e:
        proc.kill()
        raise FileUnreadable("conversion timed out") from e
    out = os.path.join(work, os.path.splitext(os.path.basename(path))[0] + ".xlsx")
    if proc.returncode != 0 or not os.path.exists(out):
        raise FileUnreadable("conversion failed")
    return out


def detect_encoding(path: str) -> tuple[str, float | None]:
    """(encoding, confidence) without loading the file (spec §4.2): BOM, UTF-16 by NUL density in the first
    4 KB, UTF-8 validated incrementally over the whole file, else a charset guess on the first 1 MB."""
    with open(path, "rb") as f:
        head = f.read(4096)
        if head.startswith(codecs.BOM_UTF8):
            return "utf-8-sig", None
        if head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
            return "utf-16", None                   # Python reads the BOM and picks the byte order
        if len(head) >= 2:
            odd, even = head[1::2], head[0::2]
            if odd and sum(b == 0 for b in odd) / len(odd) >= 0.9:
                return "utf-16-le", None
            if even and sum(b == 0 for b in even) / len(even) >= 0.9:
                return "utf-16-be", None
        f.seek(0)
        decoder = codecs.getincrementaldecoder("utf-8")()
        try:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                decoder.decode(chunk)
            decoder.decode(b"", final=True)
            return "utf-8", None
        except UnicodeDecodeError:
            f.seek(0)
            sample = f.read(1 << 20)
    try:
        import charset_normalizer
        best = charset_normalizer.from_bytes(sample).best()
        if best is not None:
            return best.encoding, round(1.0 - best.chaos, 2)
    except ImportError:
        pass
    return "cp1252", 0.5


def sniff_delimiter(text: str) -> str:
    """Most consistent field count over sampled records (at least 2 fields); ties go to `,`."""
    sample, best, best_score = text[:65536], ",", -1.0
    for d in [",", ";", "\t", "|"]:
        counts = [len(r) for r in list(csv.reader(io.StringIO(sample, newline=""), delimiter=d))[:200] if r]
        if not counts:
            continue
        mode = max(set(counts), key=counts.count)
        if mode < 2:
            continue
        score = counts.count(mode) / len(counts) + mode * 1e-4
        if score > best_score + 1e-9:
            best, best_score = d, score
    return best


def read_text(path: str, read_opts: dict | None = None) -> tuple[Workbook, dict]:
    """CSV/TSV/TXT into one grid: rows = record numbers, columns = field positions (spec §4.2).

    The file is streamed through a decoder, never held whole in memory. Every field stays text; ragged
    records are kept (extra fields become unlabelled columns)."""
    read_opts = read_opts or {}
    if read_opts.get("encoding"):
        enc, conf = read_opts["encoding"], None
    else:
        enc, conf = detect_encoding(path)
    errors = "strict" if conf is None and not read_opts.get("encoding") else "replace"
    try:
        codecs.lookup(enc)
        with open(path, "rb") as raw:
            delim = read_opts.get("delimiter") or sniff_delimiter(
                io.TextIOWrapper(raw, encoding=enc, errors="replace", newline="").read(65536))
    except LookupError as e:
        raise FileUnreadable(f"unknown encoding {enc}") from e
    csv.field_size_limit(MAX_FIELD + 1)
    sh = Sheet(0, os.path.basename(path), "sheet_1", virtual=True)
    lens: dict[int, int] = {}
    try:
        with open(path, "rb") as raw:
            text = io.TextIOWrapper(raw, encoding=enc, errors=errors, newline="")
            for rec_no, rec in enumerate(csv.reader(text, delimiter=delim), start=1):
                if len(rec) > MAX_FIELDS:
                    raise FileUnreadable("record has more than 16,384 fields")
                if rec:
                    lens[rec_no] = len(rec)
                for col, val in enumerate(rec, start=1):
                    if val == "":
                        continue
                    if len(val) > MAX_FIELD:
                        raise FileUnreadable("field larger than 1 MB")
                    sh.cells[(rec_no, col)] = Cell(classify_text(val), val, "text")
    except csv.Error as e:
        raise FileUnreadable(f"csv parse error: {e}") from e
    except UnicodeDecodeError as e:
        raise FileUnreadable("undecodable text") from e
    if lens:
        mode = max(set(lens.values()), key=list(lens.values()).count)
        sh.ragged = {r: (n, mode) for r, n in lens.items() if n != mode}
        sh.ragged_short = sum(1 for n, _ in sh.ragged.values() if n < mode)
        sh.ragged_long = sum(1 for n, _ in sh.ragged.values() if n > mode)
    mark_period_years(sh.cells)
    return Workbook([sh]), {"encoding": enc, "confidence": conf, "delimiter": delim}


def read_parquet_footer(path: str, sample_rows: int = 10_000) -> dict:
    """Schema, row count and up to 5 distinct sample values per column (spec 4.3)."""
    import pyarrow.parquet as pq
    try:
        pf = pq.ParquetFile(path)
    except Exception as e:  # noqa: BLE001
        raise FileUnreadable(f"{type(e).__name__}: cannot read parquet footer") from e
    cols = [{"name": f.name, "type": str(f.type)} for f in pf.schema_arrow]
    samples: dict[str, list] = {c["name"]: [] for c in cols}
    seen = 0
    for batch in pf.iter_batches(batch_size=2048):
        for name, got in samples.items():
            for v in batch.column(name).to_pylist():
                if v is not None and len(got) < 5 and str(v) not in got:
                    got.append(str(v))
        seen += batch.num_rows
        if seen >= sample_rows:
            break
    return {"rows": pf.metadata.num_rows, "columns": cols, "samples": samples}
