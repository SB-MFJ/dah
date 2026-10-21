"""Ingestion: store blob -> create document version -> extract -> bronze parquet -> post-ingest hooks."""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import traceback
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Callable

from sqlalchemy import func, select

from .. import jobs, lake
from ..config import get_settings
from ..db import Document, DocumentVersion, session_scope, utcnow
from ..storage import BlobStore
from .detect import detect_mime
from .extractors import extract

log = logging.getLogger("dah.ingest")

# Called as hook(document_id, version) after a version is processed (used by on_ingest pipelines).
post_ingest_hooks: list[Callable[[str, int], None]] = []


def normalize_path(p: str) -> str:
    parts = [x for x in PurePosixPath("/" + p.replace("\\", "/")).parts if x not in ("/", ".", "..")]
    if not parts:
        raise ValueError("empty document path")
    return "/".join(parts)


def ingest_file(
    stream: BinaryIO,
    filename: str,
    logical_path: str | None = None,
    comment: str | None = None,
    created_by: str = "admin",
    user_metadata: dict | None = None,
    sync: bool = False,
) -> dict:
    """Store an upload as a new version of the document at ``logical_path`` (default: filename).

    Uploading byte-identical content to a path whose current version already has it is a no-op.
    """
    settings = get_settings()
    filename = os.path.basename(filename.replace("\\", "/")) or "upload.bin"
    path = normalize_path(logical_path or filename)
    sha, size = BlobStore().put_stream(stream, max_bytes=settings.max_upload_mb * 1024 * 1024)

    with session_scope() as s:
        doc = s.scalar(select(Document).where(Document.path == path))
        if doc is None:
            doc = Document(path=path, name=PurePosixPath(path).name, current_version=0)
            s.add(doc)
            s.flush()
        else:
            cur = doc.version_row()
            latest = max((v.version for v in doc.versions), default=0)
            if cur is not None and cur.sha256 == sha and cur.version == latest and not user_metadata:
                return _summary(doc, cur, unchanged=True)
        nxt = (s.scalar(select(func.max(DocumentVersion.version)).where(DocumentVersion.document_id == doc.id)) or 0) + 1
        v = DocumentVersion(
            document_id=doc.id,
            version=nxt,
            filename=filename,
            sha256=sha,
            size=size,
            status="pending",
            comment=comment,
            created_by=created_by,
            metadata_json="{}",
        )
        if user_metadata:
            v.meta = {"user": user_metadata}
        s.add(v)
        s.flush()
        doc_id, version_id = doc.id, v.id
        result = _summary(doc, v)

    if sync:
        process_version(version_id)
        with session_scope() as s:
            v = s.get(DocumentVersion, version_id)
            result = _summary(v.document, v)
    else:
        jobs.submit(process_version, version_id)
    result["document_id"] = doc_id
    return result


def _summary(doc: Document, v: DocumentVersion, unchanged: bool = False) -> dict:
    return {
        "document_id": doc.id,
        "path": doc.path,
        "version": v.version,
        "status": v.status,
        "sha256": v.sha256,
        "size": v.size,
        "mime": v.mime,
        "unchanged": unchanged,
    }


def process_version(version_id: int) -> None:
    """Extract text/metadata for one document version and publish it as current."""
    blobs = BlobStore()
    with session_scope() as s:
        v = s.get(DocumentVersion, version_id)
        if v is None:
            return
        v.status = "processing"
        v.error = None
        doc_id, version, sha, filename = v.document_id, v.version, v.sha256, v.filename
        user_md = v.meta.get("user")

    tmpdir = Path(tempfile.mkdtemp(prefix="dah-ingest-", dir=get_settings().data_dir / "tmp"))
    try:
        # Give the file its real name: converters (LibreOffice) rely on the extension.
        local = tmpdir / (filename or "file")
        shutil.copyfile(blobs.path(sha), local)
        mime = detect_mime(local, filename)
        status, error = "ready", None
        try:
            ex = extract(local, mime)
        except Exception as e:
            log.exception("extraction failed for %s v%s", doc_id, version)
            from .extractors import Extraction

            ex = Extraction(method="failed")
            status, error = "failed", f"{type(e).__name__}: {e}"

        text = ex.text
        md = dict(ex.metadata)
        md.update(
            {
                "filename": filename,
                "extension": Path(filename).suffix.lower().lstrip("."),
                "mime": mime,
                "size": local.stat().st_size,
                "sha256": sha,
                "page_count": len(ex.pages),
                "char_count": len(text),
                "word_count": len(text.split()),
                "ocr_pages": sum(1 for _, o in ex.pages if o),
                "extraction_method": ex.method,
                "ingested_at": utcnow().isoformat(),
            }
        )
        if ex.warnings:
            md["warnings"] = ex.warnings
        if ex.records:
            md["record_count"] = len(ex.records)
        if user_md:
            md["user"] = user_md

        bronze = lake.write_pages(doc_id, version, ex.pages)
        records = lake.write_records(doc_id, version, ex.records)

        with session_scope() as s:
            v = s.get(DocumentVersion, version_id)
            v.mime = mime
            v.text = text
            v.meta = md
            v.page_count = len(ex.pages)
            v.bronze_path = bronze
            v.records_path = records
            v.status = status
            v.error = error
            v.processed_at = utcnow()
            doc = v.document
            # Never regress: a slower, older upload must not replace a newer current version.
            if version >= doc.current_version:
                doc.current_version = version
    except Exception as e:
        with session_scope() as s:
            v = s.get(DocumentVersion, version_id)
            v.status = "failed"
            v.error = f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}"
        raise
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    for hook in post_ingest_hooks:
        try:
            hook(doc_id, version)
        except Exception:
            log.exception("post-ingest hook failed for %s v%s", doc_id, version)


def reprocess(document_id: str, version: int | None = None, sync: bool = False) -> int:
    with session_scope() as s:
        doc = s.get(Document, document_id)
        if doc is None:
            raise KeyError(document_id)
        v = doc.version_row(version) or (doc.versions[-1] if doc.versions else None)
        if v is None:
            raise KeyError(f"{document_id} has no versions")
        vid = v.id
        v.status = "pending"
    if sync:
        process_version(vid)
    else:
        jobs.submit(process_version, vid)
    return vid


@jobs.on_startup
def recover() -> None:
    """Re-queue versions that were pending/processing when the server stopped."""
    with session_scope() as s:
        ids = s.scalars(select(DocumentVersion.id).where(DocumentVersion.status.in_(("pending", "processing")))).all()
    for vid in ids:
        jobs.submit(process_version, vid)
