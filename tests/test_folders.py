from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from .test_ingest import upload

PW = "correct horse battery"


@pytest.fixture()
def org(client):
    """Docs in finance/, hr/, public/; users ann (group finance), bob, carol, dave; grants."""
    for path, body, md in [("finance/q1.txt", b"revenue up", {"tag": "fin"}), ("finance/sub/q2.txt", b"revenue flat", None),
                           ("hr/salaries.txt", b"secret salaries", {"secret_key_name": 1}), ("hr/2026/plan.txt", b"hiring plan", None),
                           ("public/readme.txt", b"hello everyone", None)]:
        upload(client, path.split("/")[-1], body, path=path, **({"metadata": md} if md else {}))
    for name, groups in [("ann", ["finance"]), ("bob", []), ("carol", []), ("dave", [])]:
        assert client.post("/api/users", json={"username": name, "password": PW, "email": f"{name}@x.org", "groups": groups}).status_code == 201
    for g in [{"path": "finance", "principal_type": "group", "principal": "finance", "level": "read"},
              {"path": "public", "principal_type": "authenticated", "level": "read"},
              {"path": "public", "principal_type": "user", "principal": "bob", "level": "write"},
              {"path": "hr", "principal_type": "user", "principal": "carol@x.org", "level": "manage"}]:
        assert client.post("/api/folders/grants", json=g).status_code == 201
    return client


def session(username):
    from dah.api.app import create_app

    c = TestClient(create_app())
    c.__enter__()
    assert c.post("/auth/login", json={"username": username, "password": PW}).status_code == 200
    c.headers["X-DAH-CSRF"] = "1"
    return c


def paths(c):
    return sorted(d["path"] for d in c.get("/api/documents").json()["items"])


def doc_id(admin, path):
    return next(d["id"] for d in admin.get("/api/documents", params={"q": path}).json()["items"] if d["path"] == path)


def test_read_scoping(org):
    ann = session("ann")
    assert paths(ann) == ["finance/q1.txt", "finance/sub/q2.txt", "public/readme.txt"]
    hr = doc_id(org, "hr/salaries.txt")
    for url in (f"/api/documents/{hr}", f"/api/documents/{hr}/text", f"/api/documents/{hr}/download", f"/api/documents/{hr}/versions",
                f"/api/documents/{hr}/pages"):
        assert ann.get(url).status_code == 404, url  # indistinguishable from missing
    fin = doc_id(org, "finance/q1.txt")
    d = ann.get(f"/api/documents/{fin}").json()
    assert d["my_level"] == 1 and ann.get(f"/api/documents/{fin}/download").content == b"revenue up"
    assert [h["path"] for h in ann.get("/api/documents/search", params={"q": "e"}).json()["rows"]] == \
        ["finance/q1.txt", "finance/sub/q2.txt", "public/readme.txt"]
    dave = session("dave")
    assert paths(dave) == ["public/readme.txt"]  # "authenticated" grant


def test_write_and_manage_levels(org):
    ann, bob = session("ann"), session("bob")
    assert ann.post("/api/documents", files=[("files", ("x.txt", b"x"))], data={"path": "finance/x.txt"}).status_code == 403
    assert ann.post("/api/documents", files=[("files", ("x.txt", b"x"))], data={"path": "public/x.txt"}).status_code == 403
    r = bob.post("/api/documents", files=[("files", ("n.txt", b"v1"))], data={"path": "public/n.txt", "sync": "true"})
    assert r.status_code == 200 and r.json()["items"][0]["status"] == "ready"
    bob.post("/api/documents", files=[("files", ("n.txt", b"v2"))], data={"path": "public/n.txt", "sync": "true"})
    nid = doc_id(org, "public/n.txt")
    assert bob.post(f"/api/documents/{nid}/versions/1/restore", json={}).json()["new_version"] == 3
    assert bob.get(f"/api/documents/{nid}/versions").json()["items"][0]["created_by"] == "bob"
    assert bob.delete(f"/api/documents/{nid}").status_code == 403  # delete needs manage
    assert ann.post(f"/api/documents/{nid}/versions/1/restore", json={}).status_code == 403
    assert bob.post("/api/documents", files=[("files", ("h.txt", b"x"))], data={"path": "hr/h.txt"}).status_code == 403
    # multi-file upload is all-or-nothing on permissions
    r = bob.post("/api/documents", files=[("files", ("a.txt", b"a")), ("files", ("b.txt", b"b"))], data={"path": "hr/"})
    assert r.status_code == 403 and "hr/a.txt" not in paths(org)


def test_sql_is_scoped_and_sandboxed(org):
    org.post("/api/pipelines", json={"name": "all", "kind": "sql", "output_table": "everything", "code": "SELECT path FROM documents"})
    org.post("/api/pipelines/all/run", json={})
    ann = session("ann")
    q = lambda sql: ann.post("/api/query", json={"sql": sql})  # noqa: E731
    assert sorted(r["path"] for r in q("SELECT path FROM documents").json()["rows"]) == ["finance/q1.txt", "finance/sub/q2.txt", "public/readme.txt"]
    assert q("SELECT count(*) n FROM pages").json()["rows"] == [{"n": 3}]
    assert q("SELECT count(*) n FROM document_versions").json()["rows"] == [{"n": 3}]
    assert q("SELECT * FROM everything").status_code == 400  # pipeline tables hidden from non-admins
    from dah.config import get_settings

    lake = get_settings().lake_dir
    for bypass in (f"SELECT * FROM read_parquet('{lake}/bronze/*/*/pages.parquet')", f"SELECT * FROM glob('{lake}/**')",
                   f"SELECT * FROM read_parquet('{lake}/tables/everything/v1.parquet')", "SET enable_external_access=true",
                   f"COPY (SELECT 1) TO '{lake}/x.csv'"):
        r = q(bypass)
        assert r.status_code == 400, (bypass, r.text)
    assert ann.post("/api/query", json={"sql": "SELECT 1", "table_versions": {"everything": 1}}).status_code == 403
    dave = session("dave")
    assert dave.post("/api/query", json={"sql": "SELECT path FROM documents"}).json()["rows"] == [{"path": "public/readme.txt"}]
    s = ann.get("/api/schema").json()
    assert set(s["views"]) == {"documents", "document_versions", "pages", "records"}
    keys = {k["path"] for k in s["metadata_keys"]}
    assert "user.tag" in keys and "user.secret_key_name" not in keys


def test_admin_only_areas(org):
    ann = session("ann")
    for url in ("/api/pipelines", "/api/tables", "/api/users", "/api/sso/providers", "/api/data-endpoints", "/api/settings/ml", "/api/access-log"):
        assert ann.get(url).status_code == 403, url


def test_folder_managers_delegate(org):
    carol, ann = session("carol"), session("ann")
    g = carol.get("/api/folders/grants", params={"path": "hr/2026"}).json()
    assert g["grants"] == [] and g["inherited"][0]["principal"] == "carol@x.org"
    assert carol.get("/api/folders/grants", params={"path": "finance"}).status_code == 403
    r = carol.post("/api/folders/grants", json={"path": "hr/2026/", "principal_type": "user", "principal": "ann", "level": "read"})
    assert r.status_code == 201 and r.json()["path"] == "hr/2026"
    assert "hr/2026/plan.txt" in paths(ann) and "hr/salaries.txt" not in paths(ann)
    assert carol.post("/api/folders/grants", json={"path": "finance", "principal_type": "user", "principal": "carol", "level": "read"}).status_code == 403
    # upsert changes the level instead of duplicating
    carol.post("/api/folders/grants", json={"path": "hr/2026", "principal_type": "user", "principal": "ann", "level": "write"})
    grants = carol.get("/api/folders/grants", params={"path": "hr/2026"}).json()["grants"]
    assert len(grants) == 1 and grants[0]["level"] == "write"
    assert carol.delete(f"/api/folders/grants/{grants[0]['id']}").status_code == 200
    assert "hr/2026/plan.txt" not in paths(ann)
    hr = doc_id(org, "hr/salaries.txt")
    assert carol.delete(f"/api/documents/{hr}").status_code == 200  # manage may delete


def test_tree_effective_and_root_grant(org):
    ann = session("ann")
    tree = {f["path"]: f for f in ann.get("/api/folders").json()["items"]}
    assert tree["finance"]["documents"] == 2 and tree["finance"]["my_level"] == "read" and "hr" not in tree
    eff = org.get("/api/folders/effective", params={"path": "finance/sub", "user": "ann@x.org"}).json()
    assert eff["level"] == "read" and eff["via"][0]["principal"] == "finance"
    assert org.get("/api/folders/effective", params={"path": "hr", "user": "ann"}).json()["level"] is None
    assert ann.get("/api/folders/effective", params={"path": "finance", "user": "bob"}).status_code == 403
    org.post("/api/folders/grants", json={"path": "/", "principal_type": "authenticated", "level": "read"})
    assert len(paths(session("dave"))) == 5
    assert session("dave").post("/api/query", json={"sql": "SELECT * FROM everything_missing"}).status_code == 400


def test_idp_groups_and_principal_sessions(org):
    from dah.sso.core import Principal, issue_session

    tok = issue_session(Principal(subject="ext-1", provider="okta", email="ext@x.org", groups=["finance"]))
    with TestClient(__import__("dah.api.app", fromlist=["create_app"]).create_app()) as c:
        r = c.get("/api/documents", headers={"Authorization": f"Bearer {tok}"})
        assert sorted(d["path"] for d in r.json()["items"]) == ["finance/q1.txt", "finance/sub/q2.txt", "public/readme.txt"]


def test_data_endpoint_folder_column(org):
    r = org.post("/api/data-endpoints", json={"slug": "docs", "source_sql": "SELECT path, text FROM documents", "policy": {"folder_column": "path"}})
    assert r.status_code == 201, r.text
    ann = session("ann")
    assert sorted(x["path"] for x in ann.get("/data/docs").json()["rows"]) == ["finance/q1.txt", "finance/sub/q2.txt", "public/readme.txt"]
    sim = org.post("/api/data-endpoints/docs/simulate", json={"principal": {"email": "z@x.org", "groups": ["finance"], "provider": "okta"}}).json()
    assert sorted(x["path"] for x in sim["rows"]) == ["finance/q1.txt", "finance/sub/q2.txt", "public/readme.txt"]
    assert len(org.get("/data/docs").json()["rows"]) == 5  # admin sees all


def test_mcp_acts_as_user(org):
    import asyncio  # noqa: F401
    import json

    from dah import mcp_server

    from .test_mcp_assist import mcp_http_call

    ann = session("ann")
    pat = ann.post("/auth/tokens", json={"name": "mcp"}).json()["token"]
    mcp_server.set_client(org)
    mcp_server._verified.clear()
    with TestClient(mcp_server.http_app()) as c:
        res = mcp_http_call(c, pat, "list_documents")
        assert sorted(i["path"] for i in json.loads(res["content"][0]["text"])["items"]) == ["finance/q1.txt", "finance/sub/q2.txt", "public/readme.txt"]
        res = mcp_http_call(c, pat, "list_pipelines")
        assert res["isError"] is True and "403" in res["content"][0]["text"]
