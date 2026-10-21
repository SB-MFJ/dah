from __future__ import annotations

import io

import pyarrow as pa
import pyarrow.parquet as pq


def test_health_and_auth(client, anon):
    assert client.get("/healthz").json()["ok"] is True
    assert anon.post("/api/query", json={"sql": "SELECT 1"}).status_code == 401
    r = anon.post("/api/query", json={"sql": "SELECT 1"}, headers={"X-API-Key": "nope"})
    assert r.status_code == 401


def test_blob_store_dedup(env):
    from dah.storage import BlobStore

    bs = BlobStore()
    a = bs.put_stream(io.BytesIO(b"hello"))
    b = bs.put_bytes(b"hello")
    assert a == b and a[1] == 5
    assert bs.open(a[0]).read() == b"hello"


def test_builtin_views_exist_and_empty(client):
    r = client.post("/api/query", json={"sql": "SELECT count(*) AS n FROM documents"}).json()
    assert r["rows"] == [{"n": 0}]
    for v in ("document_versions", "pages", "records"):
        assert client.post("/api/query", json={"sql": f"SELECT * FROM {v}"}).status_code == 200


def test_sandbox_blocks_host_files(client):
    r = client.post("/api/query", json={"sql": "SELECT * FROM read_csv('/etc/hosts')"})
    assert r.status_code == 400 and "Permission" in r.json()["detail"]


def test_table_snapshots(env):
    from dah import lake
    from dah.db import session_scope

    from dah.config import get_settings

    tmp = get_settings().data_dir / "tmp"
    for i in (1, 2):
        p = tmp / f"stage{i}.parquet"
        pq.write_table(pa.table({"x": [i]}), p)
        with session_scope() as s:
            tv = lake.commit_table_snapshot(s, "t1", p)
            assert tv.version == i
    assert lake.run_query("SELECT x FROM t1")["rows"] == [{"x": 2}]
    assert lake.run_query("SELECT x FROM t1", con=lake.connect(table_versions={"t1": 1}))["rows"] == [{"x": 1}]
