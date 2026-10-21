"""Lakehouse query layer.

Zones:
  raw     content-addressed blobs (``storage.BlobStore``)
  bronze  per-document-version Parquet: extracted pages and tabular records
  tables  Parquet snapshots written by pipelines, one file per table version

Every query gets a fresh in-memory DuckDB connection with these views:
  documents          current version of every document (metadata as JSON)
  document_versions  every version of every document
  pages              extracted / OCR'd text per page (current versions)
  records            rows of tabular documents (csv/xlsx/...) as JSON (current versions)
  <table>            latest snapshot of every pipeline output table
File access outside the lake directory is disabled for the connection.
"""

from __future__ import annotations

import base64
import datetime as dt
import decimal
import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import get_settings
from .db import Document, DocumentVersion, LakeTable, LakeTableVersion, dumps, session_scope

IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
BUILTIN_VIEWS = ("documents", "document_versions", "pages", "records")

PAGES_SCHEMA = pa.schema(
    [("document_id", pa.string()), ("version", pa.int32()), ("page", pa.int32()), ("text", pa.string()), ("ocr", pa.bool_())]
)
RECORDS_SCHEMA = pa.schema(
    [("document_id", pa.string()), ("version", pa.int32()), ("sheet", pa.string()), ("row_number", pa.int32()), ("data", pa.string())]
)
DOC_FIELDS = [
    ("id", pa.string()),
    ("path", pa.string()),
    ("name", pa.string()),
    ("version", pa.int32()),
    ("is_current", pa.bool_()),
    ("filename", pa.string()),
    ("mime", pa.string()),
    ("size", pa.int64()),
    ("sha256", pa.string()),
    ("status", pa.string()),
    ("page_count", pa.int32()),
    ("text", pa.string()),
    ("metadata", pa.string()),
    ("created_at", pa.timestamp("us", tz="UTC")),
    ("version_created_at", pa.timestamp("us", tz="UTC")),
]
DOC_SCHEMA = pa.schema(DOC_FIELDS)


def valid_ident(name: str) -> bool:
    return bool(IDENT.match(name)) and name.lower() not in BUILTIN_VIEWS


def _q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _doc_rows(session: Session, current_only: bool) -> pa.Table:
    q = select(Document, DocumentVersion).join(DocumentVersion, DocumentVersion.document_id == Document.id)
    if current_only:
        q = q.where(DocumentVersion.version == Document.current_version)
    cols: dict[str, list] = {f: [] for f, _ in DOC_FIELDS}
    for d, v in session.execute(q):
        row = {
            "id": d.id,
            "path": d.path,
            "name": d.name,
            "version": v.version,
            "is_current": v.version == d.current_version,
            "filename": v.filename,
            "mime": v.mime,
            "size": v.size,
            "sha256": v.sha256,
            "status": v.status,
            "page_count": v.page_count,
            "text": v.text,
            "metadata": v.metadata_json or "{}",
            "created_at": d.created_at,
            "version_created_at": v.created_at,
        }
        for k in cols:
            cols[k].append(row[k])
    return pa.table(cols, schema=DOC_SCHEMA)


def _parquet_view(con: duckdb.DuckDBPyConnection, name: str, paths: list[str], schema: pa.Schema, json_cols=()) -> None:
    replace = ""
    if json_cols:
        replace = " REPLACE (" + ", ".join(f"CAST({c} AS JSON) AS {c}" for c in json_cols) + ")"
    if paths:
        plist = "[" + ", ".join(_q(p) for p in paths) + "]"
        con.execute(f'CREATE VIEW "{name}" AS SELECT *{replace} FROM read_parquet({plist}, union_by_name=true)')
    else:
        con.register(f"_empty_{name}", schema.empty_table())
        con.execute(f'CREATE VIEW "{name}" AS SELECT *{replace} FROM _empty_{name}')


def connect(session: Session | None = None, *, table_versions: dict[str, int] | None = None) -> duckdb.DuckDBPyConnection:
    """Build a sandboxed DuckDB connection over the current lake state.

    ``table_versions`` pins specific lake tables to older snapshots (time travel).
    """
    if session is None:
        with session_scope() as s:
            return connect(s, table_versions=table_versions)

    settings = get_settings()
    con = duckdb.connect(":memory:")

    con.register("_documents", _doc_rows(session, current_only=True))
    con.execute('CREATE VIEW documents AS SELECT * EXCLUDE (is_current) REPLACE (CAST(metadata AS JSON) AS metadata) FROM _documents')
    con.register("_document_versions", _doc_rows(session, current_only=False))
    con.execute("CREATE VIEW document_versions AS SELECT * REPLACE (CAST(metadata AS JSON) AS metadata) FROM _document_versions")

    cur = session.execute(
        select(DocumentVersion.bronze_path, DocumentVersion.records_path)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(DocumentVersion.version == Document.current_version)
    ).all()
    _parquet_view(con, "pages", [p for p, _ in cur if p], PAGES_SCHEMA)
    _parquet_view(con, "records", [r for _, r in cur if r], RECORDS_SCHEMA, json_cols=("data",))

    pins = table_versions or {}
    for t in session.scalars(select(LakeTable)):
        tv = t.version_row(pins.get(t.name))
        if tv is not None and Path(tv.path).exists():
            con.execute(f'CREATE VIEW "{t.name}" AS SELECT * FROM read_parquet({_q(tv.path)})')

    lake = str(settings.lake_dir.resolve()) + "/"
    tmp = str((settings.data_dir / "tmp").resolve()) + "/"
    con.execute(f"SET allowed_directories=[{_q(lake)}, {_q(tmp)}]")
    con.execute("SET enable_external_access=false")
    con.execute("SET lock_configuration=true")
    return con


# --------------------------------------------------------------------------- results


def jsonable(v: Any) -> Any:
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        return float(v)
    if isinstance(v, uuid.UUID):
        return str(v)
    if isinstance(v, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(v)).decode()
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    return str(v)


def _maybe_json(type_name: str, v: Any) -> Any:
    if type_name == "JSON" and isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def run_query(sql: str, params: list | dict | None = None, limit: int | None = 1000, con=None) -> dict:
    """Execute SQL and return ``{columns, types, rows, truncated}`` with JSON-safe values."""
    own = con is None
    con = con or connect()
    try:
        rel = con.execute(sql, params) if params else con.execute(sql)
        desc = rel.description or []
        columns = [d[0] for d in desc]
        types = [str(d[1]) for d in desc]
        rows = rel.fetchmany(limit + 1) if limit else rel.fetchall()
        truncated = bool(limit) and len(rows) > limit
        rows = rows[:limit] if limit else rows
        out = [{c: jsonable(_maybe_json(t, v)) for c, t, v in zip(columns, types, r)} for r in rows]
        return {"columns": columns, "types": types, "rows": out, "truncated": truncated}
    finally:
        if own:
            con.close()


# --------------------------------------------------------------------------- bronze writers


def bronze_dir(document_id: str, version: int) -> Path:
    d = get_settings().lake_dir / "bronze" / document_id / f"v{version}"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_pages(document_id: str, version: int, pages: list[tuple[str, bool]]) -> str | None:
    if not pages:
        return None
    path = bronze_dir(document_id, version) / "pages.parquet"
    tbl = pa.table(
        {
            "document_id": [document_id] * len(pages),
            "version": [version] * len(pages),
            "page": list(range(1, len(pages) + 1)),
            "text": [t for t, _ in pages],
            "ocr": [o for _, o in pages],
        },
        schema=PAGES_SCHEMA,
    )
    pq.write_table(tbl, path)
    return str(path.resolve())


def write_records(document_id: str, version: int, records: list[tuple[str, int, dict]]) -> str | None:
    if not records:
        return None
    path = bronze_dir(document_id, version) / "records.parquet"
    tbl = pa.table(
        {
            "document_id": [document_id] * len(records),
            "version": [version] * len(records),
            "sheet": [s for s, _, _ in records],
            "row_number": [n for _, n, _ in records],
            "data": [dumps(r) for _, _, r in records],
        },
        schema=RECORDS_SCHEMA,
    )
    pq.write_table(tbl, path)
    return str(path.resolve())


# --------------------------------------------------------------------------- table snapshots


def commit_table_snapshot(session: Session, name: str, staged: Path, run_id: int | None = None, mode: str = "replace") -> LakeTableVersion:
    """Move a staged Parquet file into the lake as the next version of ``name``.

    ``mode="append"`` unions the staged rows onto the current snapshot.
    """
    if not valid_ident(name):
        raise ValueError(f"invalid table name {name!r}")
    table = session.get(LakeTable, name)
    if table is None:
        table = LakeTable(name=name, current_version=0)
        session.add(table)
        session.flush()
    nxt = max([v.version for v in table.versions], default=0) + 1
    dest_dir = get_settings().lake_dir / "tables" / name
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"v{nxt}.parquet"

    prev = table.version_row()
    if mode == "append" and prev is not None and Path(prev.path).exists():
        new = pq.read_table(staged)
        old = pq.read_table(prev.path)
        merged = pa.concat_tables([old, new], promote_options="permissive")
        pq.write_table(merged, dest)
        staged.unlink(missing_ok=True)
    else:
        shutil.move(str(staged), dest)

    meta = pq.read_metadata(dest)
    schema = pq.read_schema(dest)
    tv = LakeTableVersion(
        table_name=name,
        version=nxt,
        path=str(dest.resolve()),
        row_count=meta.num_rows,
        schema_json=dumps([{"name": f.name, "type": str(f.type)} for f in schema]),
        pipeline_run_id=run_id,
    )
    table.versions.append(tv)
    table.current_version = nxt
    session.flush()
    return tv


def describe_views(con: duckdb.DuckDBPyConnection) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for (name,) in con.execute("SELECT view_name FROM duckdb_views() WHERE NOT internal ORDER BY view_name").fetchall():
        cols = con.execute(f'DESCRIBE "{name}"').fetchall()
        out[name] = [{"name": c[0], "type": c[1]} for c in cols]
    return out
