from __future__ import annotations

from .test_ingest import upload
from .fixtures import make_xlsx


def test_schema_for_autocomplete(client):
    upload(client, "a.txt", b"hello", metadata={"owner": "ops", "tags": {"level": 2}})
    upload(client, "s.xlsx", make_xlsx([["region", "amount"], ["eu", 1]]))
    client.post("/api/pipelines", json={"name": "p", "kind": "sql", "output_table": "stats", "code": "SELECT 1 AS one"})
    client.post("/api/pipelines/p/run", json={})
    s = client.get("/api/schema").json()

    assert {"documents", "pages", "records", "document_versions", "stats"} <= set(s["views"])
    assert {"name": "one", "type": "INTEGER"} in s["views"]["stats"]
    assert any(c["name"] == "metadata" and c["type"] == "JSON" for c in s["views"]["documents"])

    keys = {k["path"]: k for k in s["metadata_keys"]}
    assert {"mime", "word_count", "user", "user.owner", "user.tags.level", "sheet_names"} <= set(keys)
    assert keys["user.owner"]["sample"] == '"ops"' and keys["word_count"]["types"] == ["int"]
    assert {"region", "amount"} <= {k["path"] for k in s["record_keys"]}

    ctx = {m["name"]: m for m in s["api"]["ctx"]}
    assert ctx["write_table"]["signature"].startswith("write_table(name: str, data")
    assert "flush_outputs" not in ctx and "params" in ctx
    doc = {m["name"] for m in s["api"]["doc"]}
    assert {"text", "metadata", "pages", "records", "open", "blob_path"} <= doc
