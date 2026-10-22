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
from ..ingest import scan, service
from ..storage import BlobStore
from ..auth.folders import Access
from .deps import DB, Caller, Perm

router = APIRouter(prefix="/api/documents", tags=["documents"], dependencies=[Caller])


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


def get_doc(db: Session, document_id: str, access: Access | None = None, need: str = "read") -> Document:
    """Load a document the caller may access. Unreadable documents look like missing ones (404)."""
    d = db.get(Document, document_id)
    if d is None or (access is not None and not access.can_read(d.path)):
        raise HTTPException(404, "document not found")
    if access is not None and not access.can(d.path, need):
        raise HTTPException(403, f"{need} permission required on this folder")
    return d


def doc_filter(access: Access):
    """Path predicate for lake.connect. Non-admins always get a filtered, file-allowlisted connection."""
    return None if access.admin else access.can_read


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
    access: Access = Perm,
):
    user_md = None
    if metadata:
        try:
            user_md = json.loads(metadata)
        except ValueError:
            raise HTTPException(400, "metadata must be a JSON object")
        if not isinstance(user_md, dict):
            raise HTTPException(400, "metadata must be a JSON object")
    targets = []
    for f in files:
        name = f.filename or "upload.bin"
        if path and (path.endswith("/") or len(files) > 1):
            logical = path.rstrip("/") + "/" + name
        else:
            logical = path or name
        logical = service.normalize_path(logical)
        if not access.can(logical, "write"):
            raise HTTPException(403, f"write permission required for {logical!r}")
        targets.append((f, name, logical))
    if scan.enabled():
        # Scan every file before storing any, so one infected file rejects the whole upload.
        for f, name, _ in targets:
            try:
                scan.scan_stream(f.file, name)
            except scan.InfectedFile as e:
                raise HTTPException(422, str(e))
            except scan.ScanError as e:
                raise HTTPException(503, str(e))
            f.file.seek(0)
    who = access.ident.username
    return {"items": [service.ingest_file(f.file, name, logical, comment=comment, user_metadata=user_md, sync=sync, created_by=who, scan=False)
                      for f, name, logical in targets]}


@router.get("")
def list_documents(
    db: Session = DB,
    q: str | None = Query(None, description="substring match on path"),
    prefix: str | None = None,
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
    access: Access = Perm,
):
    stmt = select(Document)
    clause = access.sql_clause(Document.path)
    if clause is not None:
        stmt = stmt.where(clause)
    if q:
        stmt = stmt.where(or_(Document.path.contains(q), Document.name.contains(q)))
    if prefix:
        stmt = stmt.where(Document.path.startswith(prefix, autoescape=True))
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    docs = db.scalars(stmt.order_by(Document.updated_at.desc()).limit(limit).offset(offset)).all()
    return {"total": total, "items": [doc_dict(d) for d in docs]}


@router.get("/search")
def search(q: str = Query(..., min_length=1), limit: int = Query(50, le=500), access: Access = Perm):
    """Case-insensitive full-text substring search over extracted pages."""
    sql = """
        SELECT p.document_id, d.path, p.page,
               substr(p.text, greatest(1, strpos(lower(p.text), lower($q)) - 80), 240) AS snippet
        FROM pages p JOIN documents d ON d.id = p.document_id
        WHERE strpos(lower(p.text), lower($q)) > 0
        ORDER BY d.path, p.page LIMIT $lim
    """
    con = lake.connect(doc_filter=doc_filter(access))
    try:
        return lake.run_query(sql, {"q": q, "lim": limit}, limit=None, con=con)
    finally:
        con.close()


@router.get("/{document_id}")
def get_document(document_id: str, db: Session = DB, text: bool = False, access: Access = Perm):
    d = get_doc(db, document_id, access)
    return {**doc_dict(d, with_text=text), "my_level": access.level(d.path)}


@router.get("/{document_id}/text", response_class=PlainTextResponse)
def get_text(document_id: str, version: int | None = None, db: Session = DB, access: Access = Perm):
    return get_version(get_doc(db, document_id, access), version).text


@router.get("/{document_id}/pages")
def get_pages(document_id: str, version: int | None = None, db: Session = DB, access: Access = Perm):
    v = get_version(get_doc(db, document_id, access), version)
    if not v.bronze_path:
        return {"pages": []}
    import pyarrow.parquet as pq

    t = pq.read_table(v.bronze_path, columns=["page", "text", "ocr"])
    return {"pages": t.to_pylist()}


@router.get("/{document_id}/download")
def download(document_id: str, version: int | None = None, db: Session = DB, access: Access = Perm):
    v = get_version(get_doc(db, document_id, access), version)
    return FileResponse(BlobStore().path(v.sha256), media_type=v.mime, filename=v.filename)


@router.post("/{document_id}/reprocess")
def reprocess(document_id: str, version: int | None = None, sync: bool = False, db: Session = DB, access: Access = Perm):
    get_doc(db, document_id, access, "write")
    service.reprocess(document_id, version, sync=sync)
    db.expire_all()
    return doc_dict(get_doc(db, document_id))


@router.delete("/{document_id}")
def delete(document_id: str, db: Session = DB, access: Access = Perm):
    """Delete a document and all versions from the catalog (blobs are content-addressed and kept). Needs manage."""
    d = get_doc(db, document_id, access, "manage")
    db.delete(d)
    db.flush()
    from ..config import get_settings

    shutil.rmtree(get_settings().lake_dir / "bronze" / document_id, ignore_errors=True)
    return {"deleted": document_id}
