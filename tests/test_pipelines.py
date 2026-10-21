from __future__ import annotations

from .test_ingest import q, upload


def create(client, **body):
    r = client.post("/api/pipelines", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def run(client, name, **body):
    r = client.post(f"/api/pipelines/{name}/run", json=body)
    assert r.status_code == 200, r.text
    return r.json()


def test_sql_pipeline_materializes_table(client):
    upload(client, "a.txt", b"one two three")
    upload(client, "b.txt", b"four")
    create(client, name="by_mime", kind="sql", output_table="mime_stats",
           code="SELECT mime, count(*) n, sum(CAST(metadata->>'word_count' AS INT)) words FROM documents GROUP BY mime;")
    r = run(client, "by_mime")
    assert r["status"] == "succeeded", (r["error"], r["log"])
    assert r["outputs"]["mime_stats"] == {"version": 1, "rows": 1, "mode": "replace"}
    assert q(client, "SELECT * FROM mime_stats") == [{"mime": "text/plain", "n": 2, "words": 4}]
    tables = client.get("/api/tables").json()["items"]
    assert tables[0]["name"] == "mime_stats" and tables[0]["rows"] == 1


def test_python_pipeline_ctx_api(client):
    upload(client, "x/a.txt", b"alpha beta", path="x/a.txt", metadata={"team": "red"})
    code = '''
def run(ctx):
    out = []
    for d in ctx.documents("(metadata->'user'->>'team') = $1", [ctx.params["team"]]):
        out.append({"path": d.path, "pages": len(d.pages), "first": d.pages[0], "bytes": len(d.open().read())})
    ctx.log("rows", len(out))
    ctx.write_table("audit", [{"n": len(out)}])
    return out
'''
    create(client, name="py1", kind="python", output_table="team_docs", code=code, params={"team": "red"})
    r = run(client, "py1")
    assert r["status"] == "succeeded", r["log"] + str(r["error"])
    assert "rows 1" in r["log"]
    assert q(client, "SELECT * FROM team_docs") == [{"path": "x/a.txt", "pages": 1, "first": "alpha beta", "bytes": 10}]
    assert q(client, "SELECT n FROM audit") == [{"n": 1}]
    # params override at run time
    r = run(client, "py1", params={"team": "blue"})
    assert r["status"] == "succeeded" and q(client, "SELECT count(*) c FROM team_docs") == [{"c": 0}]


def test_pipeline_error_is_reported(client):
    create(client, name="bad", kind="python", output_table="t", code="def run(ctx):\n    raise RuntimeError('boom')\n")
    r = run(client, "bad")
    assert r["status"] == "failed" and "boom" in r["error"]


def test_worker_does_not_see_secrets(client):
    code = "import os\ndef run(ctx):\n    return [{'tok': os.environ.get('DAH_ADMIN_TOKEN'), 'key': os.environ.get('DAH_SECRET_KEY')}]\n"
    create(client, name="peek", kind="python", output_table="peek", code=code)
    assert run(client, "peek")["status"] == "succeeded"
    assert q(client, "SELECT * FROM peek") == [{"tok": None, "key": None}]


def test_on_ingest_transform_and_backfill(client):
    upload(client, "old.txt", b"Invoice total $1,200.50 due")
    code = '''
import re

def transform(doc, ctx):
    m = re.findall(r"\\$([0-9,.]+)", doc.text)
    return {"amount": float(m[0].replace(",", "")) if m else None, "mime": "hacked"}
'''
    create(client, name="amounts", kind="python", trigger="on_ingest", code=code)
    # new uploads are enriched automatically
    upload(client, "new.txt", b"Pay $99 now")
    rows = {r["name"]: r for r in q(client, "SELECT name, metadata->>'amount' a, mime, metadata->>'mime' m FROM documents")}
    assert rows["new.txt"]["a"] == "99.0" and rows["old.txt"]["a"] is None
    assert rows["new.txt"]["m"] == "text/plain"  # protected key untouched
    # backfill existing
    r = client.post("/api/pipelines/amounts/backfill").json()
    assert r["status"] == "succeeded" and r["outputs"]["_metadata_updates"] == 2
    rows = {r["name"]: r["a"] for r in q(client, "SELECT name, metadata->>'amount' a FROM documents")}
    assert rows == {"old.txt": "1200.5", "new.txt": "99.0"}


def test_validation(client):
    assert client.post("/api/pipelines", json={"name": "s", "kind": "sql", "code": "SELECT 1"}).status_code == 400
    assert client.post("/api/pipelines", json={"name": "s", "kind": "sql", "code": "SELECT 1", "output_table": "documents"}).status_code == 400
    p = client.post("/api/pipelines/validate", json={"kind": "python", "code": "def run(ctx)\n  pass"}).json()["problems"]
    assert p[0]["severity"] == "error" and p[0]["line"] == 1
    p = client.post("/api/pipelines/validate", json={"kind": "sql", "code": "SELECT nope FROM documents"}).json()["problems"]
    assert p and "nope" in p[0]["message"]
    assert client.post("/api/pipelines/validate", json={"kind": "sql", "code": "SELECT id FROM documents"}).json()["problems"] == []


def test_append_mode(client):
    code = "def run(ctx):\n    ctx.write_table('events', [{'n': 1}], mode='append')\n"
    create(client, name="ap", kind="python", code=code)
    run(client, "ap")
    run(client, "ap")
    assert q(client, "SELECT count(*) c FROM events") == [{"c": 2}]
