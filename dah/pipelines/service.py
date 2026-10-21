"""Pipeline CRUD (every save is a new version), execution and the on-ingest hook."""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import jobs, lake
from ..config import get_settings
from ..db import Document, DocumentVersion, Pipeline, PipelineRun, PipelineVersion, dumps, session_scope, utcnow
from ..ingest import service as ingest_service

log = logging.getLogger("dah.pipelines")

NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
KINDS = ("python", "sql")
TRIGGERS = ("manual", "on_ingest")
MAX_LOG = 1_000_000
# Ingest metadata that transforms may not overwrite.
PROTECTED_KEYS = {"filename", "extension", "mime", "size", "sha256", "page_count", "char_count", "word_count", "ocr_pages", "extraction_method", "ingested_at"}
# Secrets never reach user code.
SECRET_ENV = ("DAH_ADMIN_TOKEN", "DAH_SECRET_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")

VERSIONED_FIELDS = ("kind", "trigger", "output_table", "code", "description", "params_json")

TEMPLATES = {
    "python_run": {
        "kind": "python",
        "trigger": "manual",
        "code": '''def run(ctx):
    """Build a table from the lakehouse. Return rows (list of dicts / DataFrame) or call ctx.write_table."""
    rows = []
    for doc in ctx.documents("mime = 'application/pdf'"):
        rows.append({
            "document_id": doc.id,
            "path": doc.path,
            "pages": doc.page_count,
            "author": doc.metadata.get("author"),
        })
    ctx.log(f"processed {len(rows)} documents")
    return rows
''',
    },
    "python_transform": {
        "kind": "python",
        "trigger": "on_ingest",
        "code": '''import re


def transform(doc, ctx):
    """Runs after each document is ingested. Returned dict is merged into doc.metadata."""
    amounts = [float(a.replace(",", "")) for a in re.findall(r"\\$([0-9][0-9,]*\\.?[0-9]*)", doc.text)]
    return {"amounts_found": len(amounts), "max_amount": max(amounts) if amounts else None}
''',
    },
    "sql": {
        "kind": "sql",
        "trigger": "manual",
        "code": """SELECT mime,
       count(*)                                AS documents,
       sum(size)                               AS bytes,
       sum(CAST(metadata->>'word_count' AS BIGINT)) AS words
FROM documents
GROUP BY mime
ORDER BY documents DESC
""",
    },
}


# --------------------------------------------------------------------------- CRUD


def resolve(db: Session, ref: str) -> Pipeline | None:
    return db.get(Pipeline, ref) or db.scalar(select(Pipeline).where(Pipeline.name == ref))


def _validate(fields: dict) -> None:
    if fields.get("kind") not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    if fields.get("trigger", "manual") not in TRIGGERS:
        raise ValueError(f"trigger must be one of {TRIGGERS}")
    if fields["kind"] == "sql" and fields.get("trigger") == "on_ingest":
        raise ValueError("on_ingest pipelines must be python")
    out = fields.get("output_table")
    if out and not lake.valid_ident(out):
        raise ValueError("output_table must be an identifier (letters, digits, underscore) and not a built-in view")
    if fields["kind"] == "sql" and not out:
        raise ValueError("sql pipelines need an output_table")
    if not (fields.get("code") or "").strip():
        raise ValueError("code is empty")


def create(db: Session, name: str, *, kind: str, code: str, trigger: str = "manual", output_table: str | None = None,
           description: str = "", params: dict | None = None, comment: str | None = None, created_by: str = "admin") -> Pipeline:
    if not NAME.match(name):
        raise ValueError("invalid pipeline name")
    if db.scalar(select(Pipeline).where(Pipeline.name == name)):
        raise ValueError(f"pipeline {name!r} already exists")
    fields = {"kind": kind, "trigger": trigger, "output_table": output_table or None, "code": code, "description": description or "", "params_json": dumps(params or {})}
    _validate(fields)
    p = Pipeline(name=name, current_version=1)
    p.versions.append(PipelineVersion(version=1, comment=comment or "created", created_by=created_by, **fields))
    db.add(p)
    db.flush()
    return p


def save_version(db: Session, p: Pipeline, changes: dict, comment: str | None = None, created_by: str = "admin",
                 restored_from: int | None = None) -> PipelineVersion:
    """Create a new version from the current one plus ``changes``. No-op if nothing changed."""
    cur = p.version_row()
    base = {f: getattr(cur, f) for f in VERSIONED_FIELDS} if cur else {}
    if "params" in changes:
        changes = {**changes, "params_json": dumps(changes.pop("params") or {})}
    fields = {**base, **{k: v for k, v in changes.items() if k in VERSIONED_FIELDS}}
    if fields.get("output_table") == "":
        fields["output_table"] = None
    _validate(fields)
    if cur is not None and all(fields[f] == base[f] for f in VERSIONED_FIELDS) and restored_from is None:
        return cur
    nxt = max(v.version for v in p.versions) + 1
    v = PipelineVersion(version=nxt, comment=comment, created_by=created_by, restored_from=restored_from, **fields)
    p.versions.append(v)
    p.current_version = nxt
    db.flush()
    return v


def version_dict(v: PipelineVersion, current: int) -> dict:
    return {
        "version": v.version,
        "is_current": v.version == current,
        "kind": v.kind,
        "trigger": v.trigger,
        "output_table": v.output_table,
        "code": v.code,
        "description": v.description,
        "params": v.params,
        "comment": v.comment,
        "created_by": v.created_by,
        "restored_from": v.restored_from,
        "created_at": v.created_at,
    }


def pipeline_dict(p: Pipeline, with_code: bool = True) -> dict:
    cur = p.version_row()
    d = {
        "id": p.id,
        "name": p.name,
        "enabled": p.enabled,
        "current_version": p.current_version,
        "version_count": len(p.versions),
        "created_at": p.created_at,
        "current": version_dict(cur, p.current_version) if cur else None,
    }
    if not with_code and d["current"]:
        d["current"].pop("code")
    return d


def run_dict(r: PipelineRun) -> dict:
    return {
        "id": r.id,
        "pipeline_id": r.pipeline_id,
        "pipeline_version": r.pipeline_version,
        "document_id": r.document_id,
        "status": r.status,
        "error": r.error,
        "log": r.log,
        "outputs": json.loads(r.outputs_json or "{}"),
        "started_at": r.started_at,
        "finished_at": r.finished_at,
    }


# --------------------------------------------------------------------------- execution


def _worker_env() -> dict:
    s = get_settings()
    env = {k: v for k, v in os.environ.items() if k not in SECRET_ENV}
    env["DAH_DATA_DIR"] = str(s.data_dir.resolve())
    env["DAH_DATABASE_URL"] = s.catalog_url
    return env


def _execute(job: dict) -> tuple[dict, str]:
    """Run a job in a fresh interpreter. Returns (result, combined log)."""
    s = get_settings()
    staging = Path(job["staging_dir"])
    staging.mkdir(parents=True, exist_ok=True)
    job = {**job, "result_path": str(staging / "result.json"), "timeout_s": s.pipeline_timeout_s, "memory_mb": s.pipeline_memory_mb}
    try:
        proc = subprocess.run(
            [sys.executable, "-I", "-m", "dah.pipelines.worker"],
            input=json.dumps(job),
            capture_output=True,
            text=True,
            timeout=s.pipeline_timeout_s + 10,
            env=_worker_env(),
            cwd=staging,
        )
        logtxt = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr.strip() else "")
    except subprocess.TimeoutExpired as e:
        out = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
        return {"ok": False, "error": f"timed out after {s.pipeline_timeout_s}s"}, out
    rp = Path(job["result_path"])
    if not rp.exists():
        return {"ok": False, "error": f"worker exited with code {proc.returncode} without a result"}, logtxt
    return json.loads(rp.read_text()), logtxt


def run_pipeline(ref: str, *, version: int | None = None, params: dict | None = None,
                 documents: list[tuple[str, int]] | None = None, mode: str | None = None) -> int:
    """Execute a pipeline synchronously and commit its outputs. Returns the run id."""
    with session_scope() as db:
        p = resolve(db, ref)
        if p is None:
            raise KeyError(ref)
        v = p.version_row(version)
        if v is None:
            raise KeyError(f"{ref} v{version}")
        mode = mode or ("transform" if v.trigger == "on_ingest" else "run")
        if mode == "transform" and documents is None:
            documents = [(d.id, d.current_version) for d in db.scalars(select(Document).where(Document.current_version > 0))]
        run = PipelineRun(pipeline_id=p.id, pipeline_version=v.version, status="running",
                          document_id=documents[0][0] if documents and len(documents) == 1 else None)
        db.add(run)
        db.flush()
        run_id = run.id
        job = {
            "pipeline": p.name,
            "kind": v.kind,
            "mode": mode,
            "code": v.code,
            "output_table": v.output_table,
            "params": {**v.params, **(params or {})},
            "documents": documents or [],
            "staging_dir": str(get_settings().data_dir / "tmp" / f"run-{run_id}"),
        }
        pipeline_name, pipeline_version = p.name, v.version

    try:
        result, logtxt = _execute(job)
    except Exception as e:  # pragma: no cover - spawn failure
        result, logtxt = {"ok": False, "error": f"{type(e).__name__}: {e}"}, ""

    try:
        with session_scope() as db:
            run = db.get(PipelineRun, run_id)
            outputs = {}
            if result.get("ok"):
                for table, o in (result.get("outputs") or {}).items():
                    tv = lake.commit_table_snapshot(db, table, Path(o["path"]), run_id=run_id, mode=o["mode"])
                    outputs[table] = {"version": tv.version, "rows": tv.row_count, "mode": o["mode"]}
                for doc_id, m in (result.get("metadata") or {}).items():
                    _merge_metadata(db, doc_id, m["version"], m["values"], pipeline_name, pipeline_version)
                if result.get("metadata"):
                    outputs["_metadata_updates"] = len(result["metadata"])
                if result.get("errors"):
                    outputs["_document_errors"] = result["errors"]
            run.status = "succeeded" if result.get("ok") and not result.get("errors") else "failed"
            run.error = result.get("error") or (f"{len(result['errors'])} document(s) failed" if result.get("errors") else None)
            run.log = logtxt[-MAX_LOG:]
            run.outputs_json = dumps(outputs)
            run.finished_at = utcnow()
    finally:
        shutil.rmtree(job["staging_dir"], ignore_errors=True)
    return run_id


def _merge_metadata(db: Session, doc_id: str, version: int, values: dict, pipeline: str, pipeline_version: int) -> None:
    v = db.scalar(select(DocumentVersion).where(DocumentVersion.document_id == doc_id, DocumentVersion.version == version))
    if v is None:
        return
    md = v.meta
    for k, val in values.items():
        if k in PROTECTED_KEYS:
            continue
        md[k] = val
    md.setdefault("pipelines", {})[pipeline] = {"version": pipeline_version, "keys": sorted(k for k in values if k not in PROTECTED_KEYS)}
    v.meta = md


def submit_run(ref: str, **kw) -> None:
    jobs.submit(run_pipeline, ref, **kw)


def on_ingest(document_id: str, version: int) -> None:
    with session_scope() as db:
        names = [p.name for p in db.scalars(select(Pipeline).where(Pipeline.enabled.is_(True)))
                 if (cv := p.version_row()) is not None and cv.trigger == "on_ingest"]
    for name in sorted(names):
        run_pipeline(name, documents=[(document_id, version)], mode="transform")


if on_ingest not in ingest_service.post_ingest_hooks:
    ingest_service.post_ingest_hooks.append(on_ingest)


# --------------------------------------------------------------------------- validation


def validate(kind: str, code: str) -> list[dict]:
    """Static checks for the editor: syntax errors (python) / binder errors (sql)."""
    problems: list[dict] = []
    if kind == "python":
        import ast

        try:
            tree = ast.parse(code)
        except SyntaxError as e:
            return [{"line": e.lineno or 1, "column": (e.offset or 1), "message": e.msg, "severity": "error"}]
        fns = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        if not fns & {"run", "transform"}:
            problems.append({"line": 1, "column": 1, "message": "define run(ctx) or transform(doc, ctx)", "severity": "warning"})
    elif kind == "sql":
        con = lake.connect()
        try:
            con.execute("EXPLAIN " + code.strip().rstrip(";"))
        except Exception as e:
            msg = str(e)
            m = re.search(r"LINE (\d+):", msg)
            problems.append({"line": int(m.group(1)) if m else 1, "column": 1, "message": msg.split("\n")[0], "severity": "error"})
        finally:
            con.close()
    return problems
