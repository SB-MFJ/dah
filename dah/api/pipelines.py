"""Pipeline + lake table API."""

from __future__ import annotations

import json
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import LakeTable, PipelineRun
from ..pipelines import service
from .deps import DB, Admin

router = APIRouter(prefix="/api", tags=["pipelines"], dependencies=[Admin])


class PipelineIn(BaseModel):
    name: str
    kind: Literal["python", "sql"]
    code: str
    trigger: Literal["manual", "on_ingest"] = "manual"
    output_table: str | None = None
    description: str = ""
    params: dict = Field(default_factory=dict)
    comment: str | None = None


class PipelineUpdate(BaseModel):
    kind: Literal["python", "sql"] | None = None
    code: str | None = None
    trigger: Literal["manual", "on_ingest"] | None = None
    output_table: str | None = None
    description: str | None = None
    params: dict | None = None
    comment: str | None = None
    enabled: bool | None = None


class RunIn(BaseModel):
    params: dict = Field(default_factory=dict)
    version: int | None = None
    sync: bool = True


class ValidateIn(BaseModel):
    kind: Literal["python", "sql"]
    code: str


def _get(db: Session, ref: str):
    p = service.resolve(db, ref)
    if p is None:
        raise HTTPException(404, "pipeline not found")
    return p


@router.get("/pipelines/templates")
def templates():
    return service.TEMPLATES


@router.post("/pipelines/validate")
def validate(body: ValidateIn):
    return {"problems": service.validate(body.kind, body.code)}


@router.get("/pipelines")
def list_pipelines(db: Session = DB):
    from ..db import Pipeline

    return {"items": [service.pipeline_dict(p, with_code=False) for p in db.scalars(select(Pipeline).order_by(Pipeline.name))]}


@router.post("/pipelines", status_code=201)
def create_pipeline(body: PipelineIn, db: Session = DB):
    p = service.create(db, body.name, kind=body.kind, code=body.code, trigger=body.trigger, output_table=body.output_table,
                       description=body.description, params=body.params, comment=body.comment)
    return service.pipeline_dict(p)


@router.get("/pipelines/{ref}")
def get_pipeline(ref: str, db: Session = DB):
    return service.pipeline_dict(_get(db, ref))


@router.put("/pipelines/{ref}")
def update_pipeline(ref: str, body: PipelineUpdate, db: Session = DB):
    p = _get(db, ref)
    changes = body.model_dump(exclude_unset=True)
    comment = changes.pop("comment", None)
    if "enabled" in changes:
        p.enabled = bool(changes.pop("enabled"))
    if changes:
        service.save_version(db, p, changes, comment=comment)
    return service.pipeline_dict(p)


@router.delete("/pipelines/{ref}")
def delete_pipeline(ref: str, db: Session = DB):
    p = _get(db, ref)
    db.delete(p)
    return {"deleted": p.name}


@router.post("/pipelines/{ref}/run")
def run_pipeline(ref: str, body: RunIn | None = None, db: Session = DB):
    body = body or RunIn()
    p = _get(db, ref)
    if body.sync:
        db.close()
        run_id = service.run_pipeline(p.id, version=body.version, params=body.params)
        db.expire_all()
        return service.run_dict(db.get(PipelineRun, run_id))
    service.submit_run(p.id, version=body.version, params=body.params)
    return {"queued": True}


@router.post("/pipelines/{ref}/backfill")
def backfill(ref: str, db: Session = DB):
    """Apply an on_ingest transform to the current version of every document."""
    p = _get(db, ref)
    cur = p.version_row()
    if cur is None or cur.trigger != "on_ingest":
        raise HTTPException(400, "backfill is for on_ingest pipelines")
    db.close()
    run_id = service.run_pipeline(p.id, mode="transform")
    db.expire_all()
    return service.run_dict(db.get(PipelineRun, run_id))


@router.get("/pipelines/{ref}/runs")
def runs(ref: str, limit: int = 50, db: Session = DB):
    p = _get(db, ref)
    rows = db.scalars(select(PipelineRun).where(PipelineRun.pipeline_id == p.id).order_by(PipelineRun.id.desc()).limit(limit))
    return {"items": [service.run_dict(r) for r in rows]}


@router.get("/runs/{run_id}")
def get_run(run_id: int, db: Session = DB):
    r = db.get(PipelineRun, run_id)
    if r is None:
        raise HTTPException(404, "run not found")
    return service.run_dict(r)


@router.get("/tables")
def tables(db: Session = DB):
    items = []
    for t in db.scalars(select(LakeTable).order_by(LakeTable.name)):
        cur = t.version_row()
        items.append({
            "name": t.name,
            "current_version": t.current_version,
            "version_count": len(t.versions),
            "rows": cur.row_count if cur else 0,
            "schema": json.loads(cur.schema_json) if cur else [],
            "updated_at": cur.created_at if cur else None,
        })
    return {"items": items}
