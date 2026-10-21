"""Version history / diff / restore endpoints for documents, pipelines and lake tables."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from .. import versioning
from ..db import LakeTable
from ..pipelines import service as pipelines
from ..auth.folders import Access
from .deps import DB, Admin, Caller, Perm
from .documents import doc_dict, get_doc, version_dict

router = APIRouter(prefix="/api", tags=["versions"], dependencies=[Caller])


class RestoreIn(BaseModel):
    comment: str | None = None


def _nf(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except versioning.NotFound as e:
        raise HTTPException(404, f"not found: {e}")


# ---- documents


@router.get("/documents/{document_id}/versions")
def document_versions(document_id: str, db: Session = DB, access: Access = Perm):
    d = get_doc(db, document_id, access)
    return {"current_version": d.current_version, "items": [version_dict(v, d.current_version) for v in reversed(d.versions)]}


@router.get("/documents/{document_id}/versions/{version}")
def document_version(document_id: str, version: int, db: Session = DB, access: Access = Perm):
    d = get_doc(db, document_id, access)
    v = d.version_row(version)
    if v is None:
        raise HTTPException(404, "version not found")
    return version_dict(v, d.current_version, with_text=True)


@router.post("/documents/{document_id}/versions/{version}/restore")
def restore_document(document_id: str, version: int, body: RestoreIn | None = None, db: Session = DB, access: Access = Perm):
    get_doc(db, document_id, access, "write")
    v = _nf(versioning.restore_document, db, document_id, version, comment=(body.comment if body else None), created_by=access.ident.username)
    return {"restored_from": version, "new_version": v.version, "document": doc_dict(get_doc(db, document_id))}


@router.get("/documents/{document_id}/diff")
def document_diff(document_id: str, a: int, b: int, db: Session = DB, access: Access = Perm):
    get_doc(db, document_id, access)
    return _nf(versioning.diff_documents, db, document_id, a, b)


# ---- pipelines


def _pipeline(db: Session, ref: str):
    p = pipelines.resolve(db, ref)
    if p is None:
        raise HTTPException(404, "pipeline not found")
    return p


@router.get("/pipelines/{ref}/versions", dependencies=[Admin])
def pipeline_versions(ref: str, db: Session = DB):
    p = _pipeline(db, ref)
    return {"current_version": p.current_version, "items": [pipelines.version_dict(v, p.current_version) for v in reversed(p.versions)]}


@router.get("/pipelines/{ref}/versions/{version}", dependencies=[Admin])
def pipeline_version(ref: str, version: int, db: Session = DB):
    p = _pipeline(db, ref)
    v = p.version_row(version)
    if v is None:
        raise HTTPException(404, "version not found")
    return pipelines.version_dict(v, p.current_version)


@router.post("/pipelines/{ref}/versions/{version}/restore", dependencies=[Admin])
def restore_pipeline(ref: str, version: int, body: RestoreIn | None = None, db: Session = DB):
    v = _nf(versioning.restore_pipeline, db, ref, version, comment=(body.comment if body else None))
    return {"restored_from": version, "new_version": v.version, "pipeline": pipelines.pipeline_dict(_pipeline(db, ref))}


@router.get("/pipelines/{ref}/diff", dependencies=[Admin])
def pipeline_diff(ref: str, a: int, b: int, db: Session = DB):
    return _nf(versioning.diff_pipeline, db, ref, a, b)


# ---- lake tables


@router.get("/tables/{name}/versions", dependencies=[Admin])
def table_versions(name: str, db: Session = DB):
    t = db.get(LakeTable, name)
    if t is None:
        raise HTTPException(404, "table not found")
    return {"current_version": t.current_version, "items": [versioning.table_version_dict(v, t.current_version) for v in reversed(t.versions)]}


@router.post("/tables/{name}/versions/{version}/restore", dependencies=[Admin])
def restore_table(name: str, version: int, db: Session = DB):
    v = _nf(versioning.restore_table, db, name, version)
    return {"restored_from": version, "new_version": v.version}
