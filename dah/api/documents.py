"""Document ingestion + retrieval API."""

from __future__ import annotations

import json
import shutil

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from .. import lake
from ..db import Document, DocumentVersion
from ..ingest import service
from ..storage import BlobStore
from .deps import DB, Admin

router = APIRouter(prefix="/api/documents", tags=["documents"], dependencies=[Admin])


def version_dict(v: DocumentVersion, current: int, with_text: bool = False) -> dict:
    d = {
        "version": v.version,
        "is_current": v.version == current,
        "filename": v.filename,
        "mime": v.mime,
        "size": v.size,
        "sha256": v.sha256,
        "status": v.status,
        "error": v.error,
        "page_count": v.page_count,
        "metadata": v.meta,
        "comment": v.comment,
        "created_by": v.created_by,
        "restored_from": v.restored_from,
        "created_at": v.created_at,
        "processed_at": v.processed_at,
    }
    if with_text:
        d["text"] = v.text
    return d


def doc_dict(d: Document, with_text: bool = False) -> dict:
    cur = d.version_row()
    latest = d.versions[-1] if d.versions else None
    return {
        "id": d.id,
        "path": d.path,
        "name": d.name,
        "current_version": d.current_version,
        "latest_version": latest.version if latest else 0,
        "version_count": len(d.versions),
        "created_at": d.created_at,
        "updated_at": d.updated_at,
        "current": version_dict(cur, d.current_version, with_text) if cur else None,
        "latest_status": latest.status if latest else None,
    }


def get_doc(db: Session, document_id: str) -> Document:
    d = db.get(Document, document_id)
    if d is None:
        raise HTTPException(404, "document not found")
    return d


def get_version(d: Document, version: int | None) -> DocumentVersion:
    v = d.version_row(version) if (version or d.current_version) else (d.versions[-1] if d.versions else None)
    if v is None:
        raise HTTPException(404, "version not found")
    return v


@router.post("")
def upload(
    files: list[UploadFile] = File(...),
    path: str | None = Form(None, description="logical path (single file) or folder prefix ending in '/'"),
    comment: str | None = Form(None),
    metadata: str | None = Form(None, description="JSON object stored under metadata.user"),
    sync: bool = Form(False, description="process before returning"),
):
    user_md = None
    if metadata:
        try:
            user_md = json.loads(metadata)
        except ValueError:
            raise HTTPException(400, "metadata must be a JSON object")
        if not isinstance(user_md, dict):
            raise HTTPException(400, "metadata must be a JSON object")
    out = []
    for f in files:
        name = f.filename or "upload.bin"
        if path and (path.endswith("/") or len(files) > 1):
            logical = path.rstrip("/") + "/" + name
        else:
            logical = path or name
        out.append(service.ingest_file(f.file, name, logical, comment=comment, user_metadata=user_md, sync=sync))
    return {"items": out}


@router.get("")
def list_documents(
    db: Session = DB,
    q: str | None = Query(None, description="substring match on path"),
    prefix: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    stmt = select(Document)
    if q:
        stmt = stmt.where(or_(Document.path.contains(q), Document.name.contains(q)))
    if prefix:
        stmt = stmt.where(Document.path.startswith(prefix))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    docs = db.scalars(stmt.order_by(Document.updated_at.desc()).limit(limit).offset(offset)).all()
    return {"total": total, "items": [doc_dict(d) for d in docs]}


@router.get("/search")
def search(q: str = Query(..., min_length=1), limit: int = Query(50, le=500)):
    """Case-insensitive full-text substring search over extracted pages."""
    sql = """
        SELECT p.document_id, d.path, p.page,
               substr(p.text, greatest(1, strpos(lower(p.text), lower($q)) - 80), 240) AS snippet
        FROM pages p JOIN documents d ON d.id = p.document_id
        WHERE strpos(lower(p.text), lower($q)) > 0
        ORDER BY d.path, p.page LIMIT $lim
    """
    return lake.run_query(sql, {"q": q, "lim": limit}, limit=None)


@router.get("/{document_id}")
def get_document(document_id: str, db: Session = DB, text: bool = False):
    return doc_dict(get_doc(db, document_id), with_text=text)


@router.get("/{document_id}/text", response_class=PlainTextResponse)
def get_text(document_id: str, version: int | None = None, db: Session = DB):
    return get_version(get_doc(db, document_id), version).text


@router.get("/{document_id}/pages")
def get_pages(document_id: str, version: int | None = None, db: Session = DB):
    v = get_version(get_doc(db, document_id), version)
    if not v.bronze_path:
        return {"pages": []}
    import pyarrow.parquet as pq

    t = pq.read_table(v.bronze_path, columns=["page", "text", "ocr"])
    return {"pages": t.to_pylist()}


@router.get("/{document_id}/download")
def download(document_id: str, version: int | None = None, db: Session = DB):
    v = get_version(get_doc(db, document_id), version)
    return FileResponse(BlobStore().path(v.sha256), media_type=v.mime, filename=v.filename)


@router.post("/{document_id}/reprocess")
def reprocess(document_id: str, version: int | None = None, sync: bool = False, db: Session = DB):
    get_doc(db, document_id)
    service.reprocess(document_id, version, sync=sync)
    db.expire_all()
    return doc_dict(get_doc(db, document_id))


@router.delete("/{document_id}")
def delete(document_id: str, db: Session = DB):
    """Delete a document and all versions from the catalog (blobs are content-addressed and kept)."""
    d = get_doc(db, document_id)
    db.delete(d)
    db.flush()
    from ..config import get_settings

    shutil.rmtree(get_settings().lake_dir / "bronze" / document_id, ignore_errors=True)
    return {"deleted": document_id}
