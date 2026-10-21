"""MCP server for dah.

Exposes the lakehouse to MCP clients (Claude Desktop, Claude Code, IDEs): browse and search
documents, run SQL, inspect the schema, create / edit / run pipelines (optionally with the
configured ML assistant), and list / restore versions.

It is a thin client of the dah REST API, so it can run in its own container:
  DAH_API_URL     dah API base URL (default http://localhost:8000)
  DAH_MCP_TOKEN   token the API accepts (defaults to DAH_ADMIN_TOKEN)

Transports: stdio (local clients) or streamable HTTP at /mcp. HTTP clients must send
``Authorization: Bearer <DAH_MCP_TOKEN>``.
"""

from __future__ import annotations

import argparse
import hmac
import json
import os
from typing import Any, Literal

import httpx
from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

mcp = MCPServer(
    "dah",
    instructions=(
        "dah is a document lakehouse. Documents (PDF, Office, images via OCR, text, CSV...) are queryable "
        "with DuckDB SQL through views: documents, document_versions, pages, records, plus pipeline output "
        "tables. Call describe_schema first to see tables, columns and metadata keys. Metadata is JSON: "
        "use (metadata->>'key') in SQL. Pipelines are versioned; every update creates a new version and "
        "restore_version never deletes history."
    ),
)

_client: httpx.Client | None = None


def api() -> httpx.Client:
    global _client
    if _client is None:
        token = os.environ.get("DAH_MCP_TOKEN") or os.environ.get("DAH_ADMIN_TOKEN", "")
        _client = httpx.Client(
            base_url=os.environ.get("DAH_API_URL", "http://localhost:8000"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(900, connect=10),
        )
    return _client


def set_client(client: httpx.Client) -> None:
    """Point the tools at another client (tests use the FastAPI TestClient)."""
    global _client
    _client = client


def _call(method: str, path: str, **kw) -> Any:
    r = api().request(method, path, **kw)
    if r.status_code >= 400:
        try:
            detail = r.json().get("detail")
        except ValueError:
            detail = r.text
        raise RuntimeError(f"{method} {path} failed ({r.status_code}): {detail}")
    ct = r.headers.get("content-type", "")
    return r.json() if "json" in ct else r.text


def _brief_doc(d: dict) -> dict:
    cur = d.get("current") or {}
    return {
        "id": d["id"], "path": d["path"], "version": d["current_version"], "latest_version": d["latest_version"],
        "mime": cur.get("mime"), "status": cur.get("status"), "size": cur.get("size"), "pages": cur.get("page_count"),
        "updated_at": d.get("updated_at"),
    }


# --------------------------------------------------------------------------- documents & queries


@mcp.tool(annotations=READ)
def list_documents(query: str = "", limit: int = 50, offset: int = 0) -> dict:
    """List documents (current versions), optionally filtered by a substring of the path."""
    params = {"limit": limit, "offset": offset}
    if query:
        params["q"] = query
    r = _call("GET", "/api/documents", params=params)
    return {"total": r["total"], "items": [_brief_doc(d) for d in r["items"]]}


@mcp.tool(annotations=READ)
def get_document(document_id: str, version: int | None = None, include_text: bool = False, max_chars: int = 20000) -> dict:
    """Metadata of a document (current or a specific version), optionally with extracted text."""
    if version is None:
        d = _call("GET", f"/api/documents/{document_id}", params={"text": include_text})
        v = d["current"] or {}
    else:
        d = _call("GET", f"/api/documents/{document_id}")
        v = _call("GET", f"/api/documents/{document_id}/versions/{version}")
    out = {**_brief_doc(d), "version": v.get("version"), "metadata": v.get("metadata"), "error": v.get("error")}
    if include_text:
        text = v.get("text") or ""
        out["text"] = text[:max_chars]
        out["text_truncated"] = len(text) > max_chars
    return out


@mcp.tool(annotations=READ)
def search_text(query: str, limit: int = 20) -> list[dict]:
    """Case-insensitive full-text search over extracted / OCR'd pages. Returns path, page and a snippet."""
    return _call("GET", "/api/documents/search", params={"q": query, "limit": limit})["rows"]


@mcp.tool(annotations=READ)
def run_sql(sql: str, limit: int = 200, table_versions: dict[str, int] | None = None) -> dict:
    """Run DuckDB SQL over the lakehouse. table_versions pins lake tables to older snapshots, e.g. {"stats": 2}."""
    return _call("POST", "/api/query", json={"sql": sql, "limit": limit, "table_versions": table_versions or {}})


@mcp.tool(annotations=READ)
def describe_schema(max_metadata_keys: int = 200) -> dict:
    """Tables/views with columns, document metadata keys (types, counts, samples), record keys and the pipeline ctx/doc API."""
    s = _call("GET", "/api/schema")
    return {
        "views": {k: [f"{c['name']} {c['type']}" for c in v] for k, v in s["views"].items()},
        "metadata_keys": s["metadata_keys"][:max_metadata_keys],
        "record_keys": [k["path"] for k in s["record_keys"]][:max_metadata_keys],
        "ctx_api": [m["signature"] for m in s["api"]["ctx"]],
        "doc_api": [m["signature"] for m in s["api"]["doc"]],
    }


@mcp.tool(annotations=READ)
def list_tables() -> list[dict]:
    """Lake tables produced by pipelines, with row counts, schema and current snapshot version."""
    return _call("GET", "/api/tables")["items"]


# --------------------------------------------------------------------------- pipelines


@mcp.tool(annotations=READ)
def list_pipelines() -> list[dict]:
    """All pipelines with kind, trigger, output table and current version."""
    return [
        {"name": p["name"], "enabled": p["enabled"], "version": p["current_version"], **{k: p["current"][k] for k in ("kind", "trigger", "output_table", "description")}}
        for p in _call("GET", "/api/pipelines")["items"]
    ]


@mcp.tool(annotations=READ)
def get_pipeline(name: str, version: int | None = None) -> dict:
    """A pipeline's code and settings (current version unless given)."""
    if version is None:
        return _call("GET", f"/api/pipelines/{name}")
    return _call("GET", f"/api/pipelines/{name}/versions/{version}")


@mcp.tool(annotations=READ)
def validate_pipeline(kind: Literal["python", "sql"], code: str) -> list[dict]:
    """Static check of pipeline code: Python syntax / required entry point, or DuckDB binding errors for SQL."""
    return _call("POST", "/api/pipelines/validate", json={"kind": kind, "code": code})["problems"]


@mcp.tool(annotations=WRITE)
def create_pipeline(name: str, kind: Literal["python", "sql"], code: str, trigger: Literal["manual", "on_ingest"] = "manual",
                    output_table: str | None = None, description: str = "", params: dict | None = None) -> dict:
    """Create a pipeline (version 1). SQL pipelines need output_table; on_ingest pipelines must be python with transform(doc, ctx)."""
    return _call("POST", "/api/pipelines", json={"name": name, "kind": kind, "code": code, "trigger": trigger,
                                                 "output_table": output_table, "description": description, "params": params or {}})


@mcp.tool(annotations=WRITE)
def update_pipeline(name: str, code: str | None = None, kind: Literal["python", "sql"] | None = None,
                    trigger: Literal["manual", "on_ingest"] | None = None, output_table: str | None = None,
                    description: str | None = None, params: dict | None = None, comment: str | None = None) -> dict:
    """Save a new version of a pipeline with the given changes (unchanged fields carry over)."""
    body = {k: v for k, v in {"code": code, "kind": kind, "trigger": trigger, "output_table": output_table,
                              "description": description, "params": params, "comment": comment}.items() if v is not None}
    return _call("PUT", f"/api/pipelines/{name}", json=body)


@mcp.tool(annotations=WRITE)
def run_pipeline(name: str, params: dict | None = None, version: int | None = None) -> dict:
    """Run a pipeline now and wait for it. on_ingest pipelines are applied to every current document (backfill)."""
    p = _call("GET", f"/api/pipelines/{name}")
    if p["current"]["trigger"] == "on_ingest" and version is None:
        return _call("POST", f"/api/pipelines/{name}/backfill")
    return _call("POST", f"/api/pipelines/{name}/run", json={"params": params or {}, "version": version, "sync": True})


@mcp.tool(annotations=WRITE)
def assist_edit_pipeline(name: str, instruction: str, apply: bool = False) -> dict:
    """Ask dah's configured ML assistant to rewrite a pipeline from an instruction.

    Returns the proposed code, explanation and validation problems. With apply=True and no validation
    errors, the proposal is saved as a new pipeline version.
    """
    p = _call("GET", f"/api/pipelines/{name}")
    cur = p["current"]
    proposal = _call("POST", "/api/assist/pipeline", json={"kind": cur["kind"], "code": cur["code"], "instruction": instruction, "pipeline": name})
    result = {"proposal": proposal, "applied": False}
    if apply:
        if any(x["severity"] == "error" for x in proposal["problems"]):
            result["reason"] = "not applied: proposal has validation errors"
        else:
            saved = _call("PUT", f"/api/pipelines/{name}", json={"code": proposal["code"], "comment": f"AI: {instruction[:200]}"})
            result.update(applied=True, new_version=saved["current_version"])
    return result


# --------------------------------------------------------------------------- versions


_KIND_PATH = {"document": "/api/documents", "pipeline": "/api/pipelines", "table": "/api/tables"}


@mcp.tool(annotations=READ)
def list_versions(kind: Literal["document", "pipeline", "table"], ref: str) -> dict:
    """Version history of a document (id), pipeline (name) or lake table (name)."""
    return _call("GET", f"{_KIND_PATH[kind]}/{ref}/versions")


@mcp.tool(annotations=READ)
def diff_versions(kind: Literal["document", "pipeline"], ref: str, a: int, b: int) -> dict:
    """Unified diff between two versions of a document's text / metadata or a pipeline's code / settings."""
    return _call("GET", f"{_KIND_PATH[kind]}/{ref}/diff", params={"a": a, "b": b})


@mcp.tool(annotations=WRITE)
def restore_version(kind: Literal["document", "pipeline", "table"], ref: str, version: int, comment: str | None = None) -> dict:
    """Make an older version current again by appending a copy of it as a new version (history is kept)."""
    body = {"comment": comment} if kind != "table" else None
    return _call("POST", f"{_KIND_PATH[kind]}/{ref}/versions/{version}/restore", json=body)


# --------------------------------------------------------------------------- transport


class BearerAuth:
    """ASGI middleware requiring ``Authorization: Bearer <token>`` on the HTTP transport."""

    def __init__(self, app, token: str):
        self.app, self.token = app, token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            auth = dict(scope.get("headers") or {}).get(b"authorization", b"")
            ok = auth.lower().startswith(b"bearer ") and hmac.compare_digest(auth[7:].strip(), self.token)
            if not ok:
                body = json.dumps({"error": "unauthorized"}).encode()
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def http_app():
    from mcp.server.transport_security import TransportSecuritySettings

    token = os.environ.get("DAH_MCP_TOKEN") or os.environ.get("DAH_ADMIN_TOKEN")
    if not token:
        raise SystemExit("set DAH_MCP_TOKEN or DAH_ADMIN_TOKEN before exposing MCP over HTTP")
    # Bearer auth protects the endpoint, so host-header based DNS-rebinding checks are not needed
    # (and would reject container hostnames).
    app = mcp.streamable_http_app(transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))
    return BearerAuth(app, token)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="dah-mcp")
    p.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8001)
    a = p.parse_args(argv)
    if a.transport == "stdio":
        mcp.run("stdio")
    else:
        import uvicorn

        uvicorn.run(http_app(), host=a.host, port=a.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
