from __future__ import annotations

from .test_ingest import q, upload


def test_document_versions_and_restore(client):
    a = upload(client, "policy.txt", b"version one text", path="hr/policy.txt", comment="first")
    b = upload(client, "policy.txt", b"version two text", path="hr/policy.txt", comment="second")
    doc_id = a["document_id"]
    assert b["document_id"] == doc_id and b["version"] == 2
    assert q(client, "SELECT text, version FROM documents") == [{"text": "version two text", "version": 2}]
    assert q(client, "SELECT count(*) n FROM document_versions") == [{"n": 2}]

    vs = client.get(f"/api/documents/{doc_id}/versions").json()
    assert [v["version"] for v in vs["items"]] == [2, 1] and vs["items"][0]["is_current"]
    assert client.get(f"/api/documents/{doc_id}/download", params={"version": 1}).content == b"version one text"
    assert client.get(f"/api/documents/{doc_id}/versions/1").json()["text"] == "version one text"

    diff = client.get(f"/api/documents/{doc_id}/diff", params={"a": 1, "b": 2}).json()
    assert "-version one text" in diff["text_diff"] and "+version two text" in diff["text_diff"]
    assert "sha256" in diff["metadata_diff"]["changed"]

    r = client.post(f"/api/documents/{doc_id}/versions/1/restore", json={"comment": "rollback"}).json()
    assert r["new_version"] == 3 and r["restored_from"] == 1
    assert q(client, "SELECT text, version FROM documents") == [{"text": "version one text", "version": 3}]
    assert q(client, "SELECT page, text FROM pages") == [{"page": 1, "text": "version one text"}]
    v3 = client.get(f"/api/documents/{doc_id}/versions").json()["items"][0]
    assert v3["restored_from"] == 1 and v3["comment"] == "rollback"
    # history intact; restoring the restore works too
    assert client.post(f"/api/documents/{doc_id}/versions/2/restore").json()["new_version"] == 4
    assert q(client, "SELECT text FROM documents") == [{"text": "version two text"}]
    assert client.post(f"/api/documents/{doc_id}/versions/99/restore").status_code == 404


def test_pipeline_versions_diff_restore(client):
    client.post("/api/pipelines", json={"name": "p", "kind": "sql", "output_table": "t", "code": "SELECT 1 AS x"})
    r = client.put("/api/pipelines/p", json={"code": "SELECT 2 AS x", "comment": "bump"}).json()
    assert r["current_version"] == 2
    # saving identical content does not create a version
    assert client.put("/api/pipelines/p", json={"code": "SELECT 2 AS x"}).json()["current_version"] == 2

    vs = client.get("/api/pipelines/p/versions").json()["items"]
    assert [v["version"] for v in vs] == [2, 1] and vs[0]["comment"] == "bump"
    diff = client.get("/api/pipelines/p/diff", params={"a": 1, "b": 2}).json()
    assert "-SELECT 1 AS x" in diff["code_diff"] and "+SELECT 2 AS x" in diff["code_diff"]

    r = client.post("/api/pipelines/p/versions/1/restore", json={}).json()
    assert r["new_version"] == 3 and r["pipeline"]["current"]["code"] == "SELECT 1 AS x"
    assert r["pipeline"]["current"]["restored_from"] == 1
    client.post("/api/pipelines/p/run", json={})
    assert q(client, "SELECT x FROM t") == [{"x": 1}]
    # run an older version explicitly without restoring
    client.post("/api/pipelines/p/run", json={"version": 2})
    assert q(client, "SELECT x FROM t") == [{"x": 2}]


def test_table_snapshots_time_travel_and_restore(client):
    client.post("/api/pipelines", json={"name": "c", "kind": "sql", "output_table": "counts", "code": "SELECT count(*) AS n FROM documents"})
    client.post("/api/pipelines/c/run", json={})
    upload(client, "a.txt", b"x")
    client.post("/api/pipelines/c/run", json={})
    assert q(client, "SELECT n FROM counts") == [{"n": 1}]
    old = client.post("/api/query", json={"sql": "SELECT n FROM counts", "table_versions": {"counts": 1}}).json()["rows"]
    assert old == [{"n": 0}]
    vs = client.get("/api/tables/counts/versions").json()
    assert vs["current_version"] == 2 and len(vs["items"]) == 2
    assert client.post("/api/tables/counts/versions/1/restore").json()["new_version"] == 3
    assert q(client, "SELECT n FROM counts") == [{"n": 0}]
