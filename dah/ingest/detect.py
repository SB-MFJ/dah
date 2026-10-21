"""File type detection: magic bytes first, extension as tie-breaker for container formats."""

from __future__ import annotations

import mimetypes
from pathlib import Path

import filetype

EXT_MIME = {
    ".txt": "text/plain",
    ".text": "text/plain",
    ".log": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".rst": "text/x-rst",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".ndjson": "application/x-ndjson",
    ".xml": "application/xml",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".html": "text/html",
    ".htm": "text/html",
    ".eml": "message/rfc822",
    ".pdf": "application/pdf",
    ".rtf": "application/rtf",
    ".doc": "application/msword",
    ".dot": "application/msword",
    ".xls": "application/vnd.ms-excel",
    ".ppt": "application/vnd.ms-powerpoint",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".odt": "application/vnd.oasis.opendocument.text",
    ".ods": "application/vnd.oasis.opendocument.spreadsheet",
    ".odp": "application/vnd.oasis.opendocument.presentation",
    ".wpd": "application/vnd.wordperfect",
    ".pages": "application/vnd.apple.pages",
}

# Magic results that only identify a container; the extension tells us what is inside.
CONTAINER_MIMES = {
    None,
    "application/zip",
    "application/x-ole-storage",
    "application/x-cfb",
    "application/msword",
    "application/vnd.ms-excel",
    "application/vnd.ms-powerpoint",
    "application/octet-stream",
    "text/plain",
}


def looks_like_text(head: bytes) -> bool:
    if not head:
        return True
    if b"\x00" in head:
        # UTF-16 with BOM is still text
        return head[:2] in (b"\xff\xfe", b"\xfe\xff")
    ctrl = sum(1 for b in head if b < 9 or (13 < b < 32))
    return ctrl / len(head) < 0.05


def detect_mime(path: Path, filename: str) -> str:
    ext = Path(filename).suffix.lower()
    ext_mime = EXT_MIME.get(ext) or mimetypes.guess_type(filename)[0]
    kind = filetype.guess(str(path))
    magic = kind.mime if kind else None

    if magic and magic not in CONTAINER_MIMES:
        return magic
    if ext_mime:
        return ext_mime
    if magic:
        return magic
    with open(path, "rb") as f:
        head = f.read(8192)
    if looks_like_text(head):
        s = head.lstrip()
        if s[:1] in (b"{", b"["):
            return "application/json"
        if s[:5].lower() in (b"<html", b"<!doc"):
            return "text/html"
        return "text/plain"
    return "application/octet-stream"
