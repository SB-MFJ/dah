"""Schema catalogue for editor autocomplete: views, columns, metadata keys and the ctx/doc API."""

from __future__ import annotations

import inspect
import json
from collections import Counter, defaultdict

from sqlalchemy import select

from . import lake
from .db import Document, DocumentVersion, session_scope

SAMPLE_DOCS = 5000


def _walk(prefix: str, value, types: dict, counts: Counter, samples: dict, depth: int = 0) -> None:
    if not isinstance(value, dict) or depth > 4:
        return
    for k, v in value.items():
        path = f"{prefix}.{k}" if prefix else str(k)
        counts[path] += 1
        types[path].add(type(v).__name__ if v is not None else "null")
        if path not in samples and v not in (None, "", [], {}) and not isinstance(v, dict):
            s = json.dumps(v, default=str)
            samples[path] = s if len(s) <= 80 else s[:77] + "..."
        _walk(path, v, types, counts, samples, depth + 1)


def _key_list(rows) -> list[dict]:
    types: dict[str, set] = defaultdict(set)
    counts: Counter = Counter()
    samples: dict[str, str] = {}
    for raw in rows:
        try:
            _walk("", json.loads(raw) if isinstance(raw, str) else raw, types, counts, samples)
        except ValueError:
            continue
    return [
        {"path": p, "types": sorted(types[p]), "count": counts[p], "sample": samples.get(p)}
        for p in sorted(counts, key=lambda x: (-counts[x], x))
    ]


def metadata_keys(doc_filter=None) -> list[dict]:
    with session_scope() as s:
        rows = s.execute(
            select(Document.path, DocumentVersion.metadata_json)
            .join(Document, Document.id == DocumentVersion.document_id)
            .where(DocumentVersion.version == Document.current_version)
            .order_by(DocumentVersion.id.desc())
            .limit(SAMPLE_DOCS * (1 if doc_filter is None else 4))
        ).all()
    return _key_list([m for path, m in rows if doc_filter is None or doc_filter(path)][:SAMPLE_DOCS])


def record_keys(con) -> list[dict]:
    rows = [r[0] for r in con.execute("SELECT CAST(data AS VARCHAR) FROM records LIMIT 5000").fetchall()]
    return _key_list(rows)


def _members(cls, skip=()) -> list[dict]:
    out = []
    for name, obj in inspect.getmembers(cls):
        if name.startswith("_") or name in skip:
            continue
        doc = inspect.getdoc(obj) or ""
        if isinstance(obj, property):
            out.append({"name": name, "kind": "property", "signature": name, "doc": doc})
        elif inspect.isfunction(obj):
            try:
                sig = str(inspect.signature(obj, eval_str=True))
            except Exception:
                sig = str(inspect.signature(obj))
            sig = sig.replace("(self, ", "(").replace("(self)", "()").replace("typing.", "").replace("pyarrow.lib.", "pa.").replace("dah.pipelines.context.", "")
            out.append({"name": name, "kind": "method", "signature": f"{name}{sig}", "doc": doc})
    return out


def api_members() -> dict:
    from .pipelines.context import Context, Doc

    ctx = _members(Context, skip=("flush_outputs",))
    ctx += [
        {"name": "params", "kind": "property", "signature": "params: dict", "doc": "Pipeline parameters (defaults merged with run-time overrides)."},
        {"name": "pipeline", "kind": "property", "signature": "pipeline: str", "doc": "Name of the running pipeline."},
        {"name": "output_table", "kind": "property", "signature": "output_table: str | None", "doc": "The pipeline's configured output table."},
        {"name": "con", "kind": "property", "signature": "con: duckdb.DuckDBPyConnection", "doc": "Raw sandboxed DuckDB connection."},
    ]
    fields = [
        ("id", "str", "Document id"),
        ("path", "str", "Logical path (unique)"),
        ("name", "str", "Base name"),
        ("version", "int", "Version number of this snapshot"),
        ("filename", "str", "Original upload filename"),
        ("mime", "str", "Detected MIME type"),
        ("size", "int", "Size in bytes"),
        ("sha256", "str", "Content hash"),
        ("status", "str", "pending | processing | ready | failed"),
        ("page_count", "int", "Number of pages / slides / sheets"),
        ("text", "str", "Full extracted text (OCR included)"),
        ("metadata", "dict", "Extracted + enriched metadata"),
        ("created_at", "datetime", "When the document was first ingested"),
    ]
    doc = [{"name": n, "kind": "field", "signature": f"{n}: {t}", "doc": d} for n, t, d in fields]
    doc += _members(Doc)
    return {"ctx": sorted(ctx, key=lambda m: m["name"]), "doc": sorted(doc, key=lambda m: m["name"])}


SQL_HINTS = [
    {"name": "metadata->>'key'", "doc": "Text value of a metadata key (wrap comparisons in parentheses)"},
    {"name": "metadata->'a'->>'b'", "doc": "Nested metadata value"},
    {"name": "json_keys(metadata)", "doc": "Top-level metadata keys"},
    {"name": "CAST(metadata->>'key' AS DOUBLE)", "doc": "Numeric metadata value"},
    {"name": "data->>'column'", "doc": "Column of a tabular record (records view)"},
    {"name": "regexp_extract(text, pattern, group)", "doc": "Regex capture from text"},
    {"name": "strpos(lower(text), 'term') > 0", "doc": "Case-insensitive substring search"},
]


def schema(doc_filter=None) -> dict:
    """``doc_filter(path)`` limits views and key statistics to documents the caller can read."""
    con = lake.connect(doc_filter=doc_filter)
    try:
        views = lake.describe_views(con)
        rkeys = record_keys(con)
    finally:
        con.close()
    return {
        "views": views,
        "builtin_views": list(lake.BUILTIN_VIEWS),
        "metadata_keys": metadata_keys(doc_filter),
        "record_keys": rkeys,
        "api": api_members(),
        "sql_hints": SQL_HINTS,
    }
