"""Version history, diffs and restore for documents, pipelines and lake tables.

Restoring never rewrites history: it appends a new version that copies the chosen one and
records ``restored_from``, so a restore can itself be undone by restoring again.
"""

from __future__ import annotations

import difflib
import json
import shutil
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import Document, DocumentVersion, LakeTable, LakeTableVersion, Pipeline, utcnow
from .pipelines import service as pipelines


class NotFound(KeyError):
    pass


def unified(a: str, b: str, a_name: str, b_name: str) -> str:
    return "".join(difflib.unified_diff((a or "").splitlines(True), (b or "").splitlines(True), a_name, b_name))


def dict_diff(a: dict, b: dict) -> dict:
    keys = sorted(set(a) | set(b))
    return {
        "added": {k: b[k] for k in keys if k not in a},
        "removed": {k: a[k] for k in keys if k not in b},
        "changed": {k: {"from": a[k], "to": b[k]} for k in keys if k in a and k in b and a[k] != b[k]},
    }


# --------------------------------------------------------------------------- documents


def _doc_version(db: Session, document_id: str, version: int) -> DocumentVersion:
    v = db.scalar(select(DocumentVersion).where(DocumentVersion.document_id == document_id, DocumentVersion.version == version))
    if v is None:
        raise NotFound(f"document {document_id} v{version}")
    return v


def restore_document(db: Session, document_id: str, version: int, comment: str | None = None, created_by: str = "admin") -> DocumentVersion:
    doc = db.get(Document, document_id)
    if doc is None:
        raise NotFound(document_id)
    src = _doc_version(db, document_id, version)
    nxt = (db.scalar(select(func.max(DocumentVersion.version)).where(DocumentVersion.document_id == document_id)) or 0) + 1
    v = DocumentVersion(
        document_id=document_id,
        version=nxt,
        filename=src.filename,
        sha256=src.sha256,
        size=src.size,
        mime=src.mime,
        status=src.status,
        error=src.error,
        metadata_json=src.metadata_json,
        text=src.text,
        page_count=src.page_count,
        bronze_path=src.bronze_path,  # bronze files are immutable per version; safe to share
        records_path=src.records_path,
        comment=comment or f"restored from v{version}",
        created_by=created_by,
        restored_from=version,
        processed_at=utcnow(),
    )
    db.add(v)
    doc.current_version = nxt
    db.flush()
    return v


def diff_documents(db: Session, document_id: str, a: int, b: int) -> dict:
    va, vb = _doc_version(db, document_id, a), _doc_version(db, document_id, b)
    return {
        "a": a,
        "b": b,
        "same_content": va.sha256 == vb.sha256,
        "text_diff": unified(va.text, vb.text, f"v{a}", f"v{b}"),
        "metadata_diff": dict_diff(va.meta, vb.meta),
    }


# --------------------------------------------------------------------------- pipelines


def restore_pipeline(db: Session, ref: str, version: int, comment: str | None = None, created_by: str = "admin"):
    p = pipelines.resolve(db, ref)
    if p is None:
        raise NotFound(ref)
    src = p.version_row(version)
    if src is None:
        raise NotFound(f"{ref} v{version}")
    changes = {f: getattr(src, f) for f in pipelines.VERSIONED_FIELDS}
    return pipelines.save_version(db, p, changes, comment=comment or f"restored from v{version}", created_by=created_by, restored_from=version)


def diff_pipeline(db: Session, ref: str, a: int, b: int) -> dict:
    p = pipelines.resolve(db, ref)
    if p is None:
        raise NotFound(ref)
    va, vb = p.version_row(a), p.version_row(b)
    if va is None or vb is None:
        raise NotFound(f"{ref} v{a}/v{b}")
    fa = {f: getattr(va, f) for f in pipelines.VERSIONED_FIELDS if f != "code"}
    fb = {f: getattr(vb, f) for f in pipelines.VERSIONED_FIELDS if f != "code"}
    return {"a": a, "b": b, "code_diff": unified(va.code, vb.code, f"v{a}", f"v{b}"), "fields_diff": dict_diff(fa, fb)}


# --------------------------------------------------------------------------- lake tables


def restore_table(db: Session, name: str, version: int) -> LakeTableVersion:
    t = db.get(LakeTable, name)
    if t is None:
        raise NotFound(name)
    src = t.version_row(version)
    if src is None or not Path(src.path).exists():
        raise NotFound(f"{name} v{version}")
    nxt = max(v.version for v in t.versions) + 1
    dest = get_settings().lake_dir / "tables" / name / f"v{nxt}.parquet"
    shutil.copyfile(src.path, dest)
    tv = LakeTableVersion(table_name=name, version=nxt, path=str(dest.resolve()), row_count=src.row_count,
                          schema_json=src.schema_json, restored_from=version)
    t.versions.append(tv)
    t.current_version = nxt
    db.flush()
    return tv


def table_version_dict(v: LakeTableVersion, current: int) -> dict:
    return {
        "version": v.version,
        "is_current": v.version == current,
        "rows": v.row_count,
        "schema": json.loads(v.schema_json or "[]"),
        "pipeline_run_id": v.pipeline_run_id,
        "restored_from": v.restored_from,
        "created_at": v.created_at,
    }
