from __future__ import annotations

import pytest

CSRF = {"X-DAH-CSRF": "1"}


def make_user(client, username="ann", password="correct horse battery", **kw):
    r = client.post("/api/users", json={"username": username, "password": password, **kw})
    assert r.status_code == 201, r.text
    return r.json()


def login(c, username="ann", password="correct horse battery", provider="local"):
    return c.post("/auth/login", json={"username": username, "password": password, "provider": provider})


def test_local_login_session_and_csrf(client, anon):
    make_user(client, email="ann@x.org", groups=["finance"])
    assert anon.get("/auth/me").status_code == 401
    r = login(anon)
    assert r.status_code == 200 and r.json()["user"]["username"] == "ann"
    assert "httponly" in r.headers["set-cookie"].lower() and "samesite=lax" in r.headers["set-cookie"].lower()
    me = anon.get("/auth/me").json()
    assert me["username"] == "ann" and me["is_admin"] is False and me["groups"] == ["finance"] and me["via"] == "cookie"
    # cookie-authenticated writes need the CSRF header
    assert anon.post("/auth/tokens", json={"name": "x"}).status_code == 403
    assert anon.post("/auth/tokens", json={"name": "x"}, headers=CSRF).status_code == 201
    anon.post("/auth/logout")
    anon.cookies.clear()
    assert anon.get("/auth/me").status_code == 401


def test_bad_credentials_no_enumeration_and_throttle(client, anon):
    make_user(client)
    a = login(anon, password="wrong password!")
    b = login(anon, username="nobody")
    assert a.status_code == b.status_code == 401 and a.json() == b.json()
    for _ in range(9):
        login(anon, password="wrong again!!")
    assert login(anon).status_code == 429  # locked even with the right password


def test_roles(client, anon):
    make_user(client)
    login(anon)
    assert anon.get("/api/pipelines").status_code == 403
    assert anon.get("/api/users").status_code == 403
    assert anon.get("/api/settings/ml").status_code == 403
    make_user(client, "boss", is_admin=True)
    anon.cookies.clear()
    login(anon, "boss")
    assert anon.get("/api/pipelines").status_code == 200
    me = anon.get("/auth/me").json()
    assert anon.put(f"/api/users/{me['user_id']}", json={"is_admin": False}, headers=CSRF).status_code == 400  # no self-demotion


def test_password_change_invalidates_sessions(client, anon):
    from fastapi.testclient import TestClient

    from dah.api.app import create_app

    make_user(client)
    login(anon)
    with TestClient(create_app()) as other:
        login(other)
        assert other.get("/auth/me").status_code == 200
        assert anon.post("/auth/password", json={"current_password": "nope", "new_password": "a brand new secret"}, headers=CSRF).status_code == 403
        assert anon.post("/auth/password", json={"current_password": "correct horse battery", "new_password": "short"}, headers=CSRF).status_code == 400
        r = anon.post("/auth/password", json={"current_password": "correct horse battery", "new_password": "a brand new secret"}, headers=CSRF)
        assert r.status_code == 200
        assert anon.get("/auth/me").status_code == 200  # fresh cookie issued to the changer
        assert other.get("/auth/me").status_code == 401  # everyone else signed out
    assert login(anon, password="a brand new secret").status_code == 200


def test_disable_user(client, anon):
    u = make_user(client)
    login(anon)
    client.put(f"/api/users/{u['id']}", json={"disabled": True})
    assert anon.get("/auth/me").status_code == 401
    anon.cookies.clear()
    assert login(anon).status_code == 403
    client.put(f"/api/users/{u['id']}", json={"disabled": False})
    assert login(anon).status_code == 200


def test_personal_api_tokens(client, anon):
    make_user(client)
    login(anon)
    t = anon.post("/auth/tokens", json={"name": "mcp", "expires_in_days": 30}, headers=CSRF).json()
    assert t["token"].startswith("dah_pat_") and t["prefix"] == t["token"][:12]
    from fastapi.testclient import TestClient

    from dah.api.app import create_app

    with TestClient(create_app()) as api:
        h = {"Authorization": f"Bearer {t['token']}"}
        me = api.get("/auth/me", headers=h).json()
        assert me["username"] == "ann" and me["via"] == "pat"
        assert api.post("/auth/tokens", json={"name": "y"}, headers=h).status_code == 201  # no CSRF needed for bearer
        listed = anon.get("/auth/tokens").json()["items"]
        assert {x["name"] for x in listed} == {"mcp", "y"} and all("token" not in x for x in listed)
        anon.delete(f"/auth/tokens/{t['id']}", headers=CSRF)
        assert api.get("/auth/me", headers=h).status_code == 401
        assert api.get("/auth/me", headers={"Authorization": "Bearer dah_pat_forged"}).status_code == 401
    # the admin token is not an account
    assert client.post("/auth/tokens", json={"name": "z"}).status_code == 400


def test_expired_token(client):
    from datetime import datetime, timedelta, timezone

    from dah.auth.identity import create_api_token
    from dah.db import User, session_scope

    make_user(client)
    with session_scope() as db:
        u = db.query(User).filter_by(username="ann").one()
        _, raw = create_api_token(db, u, "old", datetime.now(timezone.utc) - timedelta(days=1))
    assert client.get("/auth/me", headers={"Authorization": f"Bearer {raw}"}).status_code == 401


def test_bootstrap_admin_and_methods(env, monkeypatch):
    from fastapi.testclient import TestClient

    from dah import config
    from dah.api.app import create_app

    monkeypatch.setenv("DAH_BOOTSTRAP_ADMIN_USER", "root")
    monkeypatch.setenv("DAH_BOOTSTRAP_ADMIN_PASSWORD", "bootstrap-pass-123")
    config.get_settings.cache_clear()
    with TestClient(create_app()) as c:
        assert c.get("/auth/methods").json()["password"][0]["id"] == "local"
        assert login(c, "root", "bootstrap-pass-123").status_code == 200
        assert c.get("/auth/me").json()["is_admin"] is True


def test_form_login_redirects(client, anon):
    make_user(client)
    r = anon.post("/auth/login", data={"username": "ann", "password": "correct horse battery", "next": "/data"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/data"
    r = anon.post("/auth/login", data={"username": "ann", "password": "x", "next": "https://evil"}, follow_redirects=False)
    assert r.headers["location"] == "/sso?error=invalid"
    anon.cookies.clear()
    r = anon.post("/auth/login", data={"username": "ann", "password": "correct horse battery", "next": "//evil.com"}, follow_redirects=False)
    assert r.headers["location"] == "/"


@pytest.mark.parametrize("name", ["bad name", "", "-x", "a" * 300])
def test_username_validation(client, name):
    assert client.post("/api/users", json={"username": name, "password": "long enough pw"}).status_code in (400, 422)
