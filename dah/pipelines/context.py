"""The API that pipeline code sees: ``ctx`` and ``doc`` objects.

Python pipelines define either::

    def run(ctx):              # trigger = manual
        rows = ctx.sql("SELECT ...")
        return rows            # -> written to the pipeline's output table

    def transform(doc, ctx):   # trigger = on_ingest (and backfill)
        return {"invoice_total": 12.5}   # merged into doc.metadata

Keep the docstrings below accurate: they feed the editor's autocomplete.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

import pyarrow as pa
import pyarrow.parquet as pq

from .. import lake
from ..storage import BlobStore


@dataclass
class Doc:
    """One version of an ingested document."""

    id: str
    path: str
    name: str
    version: int
    filename: str
    mime: str
    size: int
    sha256: str
    status: str
    page_count: int
    text: str
    metadata: dict = field(default_factory=dict)
    created_at: Any = None

    @property
    def pages(self) -> list[str]:
        """Text of each page (OCR'd where needed), in order."""
        return [r["text"] for r in _ctx_query("SELECT text FROM _doc_pages WHERE document_id=$1 AND version=$2 ORDER BY page", [self.id, self.version])]

    @property
    def records(self) -> list[dict]:
        """Rows extracted from tabular content (csv/xlsx sheets, docx tables, json arrays)."""
        rows = _ctx_query("SELECT sheet, row_number, data FROM _doc_records WHERE document_id=$1 AND version=$2 ORDER BY sheet, row_number", [self.id, self.version])
        return [{"sheet": r["sheet"], "row_number": r["row_number"], **(r["data"] or {})} for r in rows]

    @property
    def blob_path(self) -> Path:
        """Filesystem path of the original bytes (read-only)."""
        return BlobStore().path(self.sha256)

    def open(self) -> BinaryIO:
        """Open the original file for binary reading."""
        return BlobStore().open(self.sha256)

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in ("id", "path", "name", "version", "filename", "mime", "size", "sha256", "status", "page_count", "metadata")}


_active: "Context | None" = None


def _ctx_query(sql: str, params: list) -> list[dict]:
    assert _active is not None, "no active pipeline context"
    return _active.sql(sql, params)


def _doc_from_row(r: dict) -> Doc:
    md = r.get("metadata")
    if isinstance(md, str):
        md = json.loads(md or "{}")
    return Doc(
        id=r["id"],
        path=r["path"],
        name=r["name"],
        version=r["version"],
        filename=r["filename"],
        mime=r["mime"],
        size=r["size"],
        sha256=r["sha256"],
        status=r["status"],
        page_count=r["page_count"],
        text=r["text"] or "",
        metadata=md or {},
        created_at=r.get("created_at"),
    )


class Context:
    """Pipeline runtime context passed to ``run(ctx)`` / ``transform(doc, ctx)``."""

    def __init__(self, pipeline: str, params: dict | None, staging_dir: Path, output_table: str | None = None):
        global _active
        self.pipeline = pipeline
        self.params: dict = params or {}
        self.output_table = output_table
        self._staging = staging_dir
        self._writes: dict[str, list[tuple[pa.Table, str]]] = {}
        self._logs: list[str] = []
        self.con = lake.connect()
        # Unfiltered page/record views so a Doc of any version can read its pages.
        from ..db import DocumentVersion, session_scope

        with session_scope() as s:
            rows = s.query(DocumentVersion.bronze_path, DocumentVersion.records_path).all()
        lake._parquet_view(self.con, "_doc_pages", [p for p, _ in rows if p], lake.PAGES_SCHEMA)
        lake._parquet_view(self.con, "_doc_records", [r for _, r in rows if r], lake.RECORDS_SCHEMA, json_cols=("data",))
        _active = self

    # ---- reading
    def sql(self, query: str, params: list | dict | None = None) -> list[dict]:
        """Run SQL over the lakehouse and return rows as dicts."""
        return lake.run_query(query, params, limit=None, con=self.con)["rows"]

    def df(self, query: str, params: list | dict | None = None):
        """Run SQL and return a pandas DataFrame."""
        return self.con.execute(query, params or []).df()

    def arrow(self, query: str, params: list | dict | None = None) -> pa.Table:
        """Run SQL and return a pyarrow Table."""
        return self.con.sql(query, params=params).to_arrow_table() if params else self.con.sql(query).to_arrow_table()

    def documents(self, where: str | None = None, params: list | dict | None = None, limit: int | None = None) -> list[Doc]:
        """Current version of documents, optionally filtered by a SQL ``where`` clause."""
        q = "SELECT * FROM documents" + (f" WHERE {where}" if where else "") + " ORDER BY path"
        if limit:
            q += f" LIMIT {int(limit)}"
        return [_doc_from_row(r) for r in self.sql(q, params)]

    def document(self, id_or_path: str, version: int | None = None) -> Doc | None:
        """One document by id or path (current version unless ``version`` given)."""
        if version is None:
            rows = self.sql("SELECT * FROM documents WHERE id=$1 OR path=$1", [id_or_path])
        else:
            rows = self.sql("SELECT * EXCLUDE (is_current) FROM document_versions WHERE (id=$1 OR path=$1) AND version=$2", [id_or_path, version])
        return _doc_from_row(rows[0]) if rows else None

    def read_table(self, name: str, version: int | None = None) -> list[dict]:
        """Rows of a lake table (latest snapshot, or a specific ``version``)."""
        if not lake.valid_ident(name):
            raise ValueError(f"invalid table name {name!r}")
        if version is None:
            return self.sql(f'SELECT * FROM "{name}"')
        con = lake.connect(table_versions={name: version})
        try:
            return lake.run_query(f'SELECT * FROM "{name}"', limit=None, con=con)["rows"]
        finally:
            con.close()

    # ---- writing
    def write_table(self, name: str, data: Any, mode: str = "replace") -> int:
        """Write rows (list of dicts / DataFrame / pyarrow Table) to lake table ``name``.

        ``mode`` is ``"replace"`` (new snapshot) or ``"append"`` (add to current snapshot).
        """
        if mode not in ("replace", "append"):
            raise ValueError("mode must be 'replace' or 'append'")
        if not lake.valid_ident(name):
            raise ValueError(f"invalid table name {name!r}")
        tbl = to_arrow(data)
        self._writes.setdefault(name, []).append((tbl, mode))
        return tbl.num_rows

    def log(self, *args: Any) -> None:
        """Append a line to the run log."""
        line = " ".join(str(a) for a in args)
        self._logs.append(line)
        print(line, flush=True)

    # ---- worker plumbing
    def flush_outputs(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for name, writes in self._writes.items():
            # Everything after the last replace is appended onto it.
            last_replace = max((i for i, (_, m) in enumerate(writes) if m == "replace"), default=None)
            start = last_replace if last_replace is not None else 0
            tables = [t for t, _ in writes[start:]]
            merged = pa.concat_tables(tables, promote_options="permissive") if len(tables) > 1 else tables[0]
            path = self._staging / f"{name}.parquet"
            pq.write_table(merged, path)
            out[name] = {"path": str(path), "mode": "replace" if last_replace is not None else "append", "rows": merged.num_rows}
        return out


def _jsonify_nested(rows: list[dict]) -> list[dict]:
    return [{k: (json.dumps(v, default=str) if isinstance(v, (dict, list)) else v) for k, v in r.items()} for r in rows]


def to_arrow(data: Any) -> pa.Table:
    if isinstance(data, pa.Table):
        return data
    if hasattr(data, "to_arrow_table"):  # duckdb relation
        return data.to_arrow_table()
    try:
        import pandas as pd

        if isinstance(data, pd.DataFrame):
            return pa.Table.from_pandas(data, preserve_index=False)
    except ImportError:  # pragma: no cover
        pass
    if isinstance(data, dict):
        data = [data]
    if isinstance(data, list):
        if data and isinstance(data[0], Doc):
            data = [d.to_dict() for d in data]
        try:
            return pa.Table.from_pylist(data)
        except (pa.ArrowInvalid, pa.ArrowTypeError):
            return pa.Table.from_pylist(_jsonify_nested(data))
    raise TypeError(f"cannot write {type(data).__name__} as a table; use list[dict], DataFrame or pyarrow.Table")
