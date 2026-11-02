"""File Scanner: detect the format from the bytes, never the extension (spec 3.1)."""
from __future__ import annotations

import hashlib
import os
import zipfile
from dataclasses import dataclass

OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
_U16 = lambda s: s.encode("utf-16-le")  # noqa: E731


@dataclass
class Sniffed:
    format: str            # xlsx xlsm xlsb xls ods parquet text | unsupported unreadable empty password
    detail: str = ""
    macro: bool = False


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _looks_text(head: bytes) -> bool:
    if head.startswith((b"\xef\xbb\xbf", b"\xff\xfe", b"\xfe\xff")):
        return True
    sample = head[:4096]
    if len(sample) >= 2:
        odd, even = sample[1::2], sample[0::2]
        if odd and sum(b == 0 for b in odd) / len(odd) >= 0.9:
            return True
        if even and sum(b == 0 for b in even) / len(even) >= 0.9:
            return True
    return b"\x00" not in sample


_BIFF8_BOF = b"\x09\x08\x10\x00\x00\x06\x05\x00"     # BOF record, BIFF8, workbook-globals substream
_FILEPASS = 0x002F


def _xls_encrypted(blob: bytes) -> bool:
    """An encrypted BIFF8 workbook has a FILEPASS record right after the workbook's opening BOF record."""
    i = blob.find(_BIFF8_BOF)
    return i >= 0 and len(blob) >= i + 22 and int.from_bytes(blob[i + 20:i + 22], "little") == _FILEPASS


def sniff(path: str) -> Sniffed:
    size = os.path.getsize(path)
    if size == 0:
        return Sniffed("empty")
    with open(path, "rb") as f:
        head = f.read(8192)
        f.seek(max(size - 4, 0))
        tail = f.read(4)
    if head[:4] == b"PAR1" and tail == b"PAR1":
        return Sniffed("parquet")
    if head[:4] == b"PK\x03\x04":
        try:
            with zipfile.ZipFile(path) as z:
                names = set(z.namelist())
                if "xl/workbook.xml" in names:
                    ct = z.read("[Content_Types].xml").decode("utf8", "replace")
                    macro = "macroEnabled" in ct
                    return Sniffed("xlsm" if macro else "xlsx", macro=macro)
                if "xl/workbook.bin" in names:
                    return Sniffed("xlsb")
                if "mimetype" in names and z.read("mimetype").strip() == \
                        b"application/vnd.oasis.opendocument.spreadsheet":
                    # ODF encryption: the manifest declares <manifest:encryption-data> for encrypted parts
                    manifest = z.read("META-INF/manifest.xml") if "META-INF/manifest.xml" in names else b""
                    return Sniffed("password" if b"encryption-data" in manifest else "ods")
        except zipfile.BadZipFile:
            return Sniffed("unreadable", "corrupt zip")
        return Sniffed("unsupported", "archive")
    if head[:8] == OLE2_MAGIC:
        with open(path, "rb") as f:
            blob = f.read()
        if _U16("EncryptedPackage") in blob and _U16("EncryptionInfo") in blob:
            return Sniffed("password")
        if _U16("Workbook") in blob or _U16("Book") in blob:
            return Sniffed("password" if _xls_encrypted(blob) else "xls")
        return Sniffed("unsupported", "ole2")
    if head[:4] == b"%PDF" or head[:8] == b"\x89PNG\r\n\x1a\n" or head[:3] == b"\xff\xd8\xff":
        return Sniffed("unsupported", "binary")
    if _looks_text(head):
        return Sniffed("text")
    return Sniffed("unsupported", "binary")
