from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest
from ldap3 import MOCK_SYNC, OFFLINE_SLAPD_2_4, Connection, Server

from .test_sso import FakeIdP, idp  # noqa: F401  (fixture)

BASE = "dc=example,dc=org"


@pytest.fixture()
def directory(client, monkeypatch):
    """In-memory LDAP directory (ldap3 MOCK_SYNC) + an 'corp-ldap' provider pointing at it."""
    server = Server("fake-ldap", get_info=OFFLINE_SLAPD_2_4)
    seed = Connection(server, client_strategy=MOCK_SYNC)
    add = seed.strategy.add_entry
    add(f"ou=people,{BASE}", {"objectClass": "organizationalUnit", "ou": "people"})
    add(f"ou=groups,{BASE}", {"objectClass": "organizationalUnit", "ou": "groups"})
    add(f"cn=svc,{BASE}", {"objectClass": "person", "cn": "svc", "sn": "svc", "userPassword": "svc-pass"})
    add(f"uid=ann,ou=people,{BASE}", {"objectClass": "inetOrgPerson", "uid": "ann", "cn": "Ann Lee", "sn": "Lee", "mail": "ann@example.org",
                                       "userPassword": "ann-pass", "departmentNumber": "42", "memberOf": [f"cn=finance,ou=groups,{BASE}"]})
    add(f"uid=bob,ou=people,{BASE}", {"objectClass": "inetOrgPerson", "uid": "bob", "cn": "Bob", "sn": "B", "mail": "bob@example.org", "userPassword": "bob-pass"})
    add(f"cn=dah-admins,ou=groups,{BASE}", {"objectClass": "groupOfNames", "cn": "dah-admins", "member": [f"uid=bob,ou=people,{BASE}"]})
    add(f"cn=legal,ou=groups,{BASE}", {"objectClass": "groupOfNames", "cn": "legal", "member": [f"uid=ann,ou=people,{BASE}"]})

    def connect(cfg, user, password):
        c = Connection(server, user=user, password=password, client_strategy=MOCK_SYNC)
        c.bind()
        return c

    from dah.auth import ldap

    monkeypatch.setattr(ldap, "connect", connect)
    r = client.post("/api/sso/providers", json={"id": "corp-ldap", "kind": "ldap", "display_name": "Corp directory", "config": {
        "server_url": "ldap://ldap.example.org", "bind_dn": f"cn=svc,{BASE}", "bind_password": "svc-pass",
        "user_search_base": f"ou=people,{BASE}", "group_search_base": f"ou=groups,{BASE}", "group_filter": "(member={user_dn})",
        "attribute_names": ["departmentNumber"], "admin_groups": ["dah-admins"]}})
    assert r.status_code == 201, r.text
    assert r.json()["config"]["bind_password"] == {"set": True} and "login_url" not in r.json()
    return server


def ldap_login(c, username, password):
    return c.post("/auth/login", json={"username": username, "password": password, "provider": "corp-ldap"})


def test_ldap_login_provisions_user_with_groups(client, anon, directory):
    assert [m["id"] for m in anon.get("/auth/methods").json()["password"]] == ["local", "corp-ldap"]
    r = ldap_login(anon, "ann", "ann-pass")
    assert r.status_code == 200, r.text
    me = anon.get("/auth/me").json()
    assert me["username"] == "ann@example.org" and me["provider"] == "corp-ldap" and me["is_admin"] is False
    assert me["groups"] == ["finance", "legal"]  # memberOf + group search
    users = {u["username"]: u for u in client.get("/api/users").json()["items"]}
    ann = users["ann@example.org"]
    assert ann["source"] == "corp-ldap" and ann["has_password"] is False and ann["display_name"] == "Ann Lee"
    # same identity on the next login: account reused, not duplicated
    anon.cookies.clear()
    ldap_login(anon, "ann", "ann-pass")
    assert sum(1 for u in client.get("/api/users").json()["items"] if u["source"] == "corp-ldap") == 1
    # LDAP attributes become principal attributes (usable in /data row filters)
    assert anon.get("/sso/me").json()["attributes"] == {"departmentNumber": "42"}


def test_ldap_admin_group_and_failures(client, anon, directory):
    assert ldap_login(anon, "bob", "bob-pass").status_code == 200
    assert anon.get("/auth/me").json()["is_admin"] is True  # via admin_groups
    assert anon.get("/api/users").status_code == 200
    anon.cookies.clear()
    assert ldap_login(anon, "ann", "wrong").status_code == 401
    assert ldap_login(anon, "nobody", "x").status_code == 401
    assert ldap_login(anon, "ann", "").status_code == 400
    assert ldap_login(anon, "*", "ann-pass").status_code == 401  # filter metacharacters are escaped
    assert ldap_login(anon, "ann)(uid=*", "ann-pass").status_code == 401


def test_ldap_disabled_account_blocked(client, anon, directory):
    ldap_login(anon, "ann", "ann-pass")
    uid = anon.get("/auth/me").json()["user_id"]
    client.put(f"/api/users/{uid}", json={"disabled": True})
    assert anon.get("/auth/me").status_code == 401
    anon.cookies.clear()
    assert ldap_login(anon, "ann", "ann-pass").status_code == 403


def test_ldap_form_login_on_sso_page_and_test_endpoint(client, anon, directory):
    page = anon.get("/sso").text
    assert "Corp directory" in page and "name=password" in page
    r = anon.post("/auth/login", data={"provider": "corp-ldap", "username": "ann", "password": "ann-pass", "next": "/data"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/data"
    assert client.post("/api/sso/providers/corp-ldap/test").json() == {"ok": True, "error": None}
    assert anon.get("/sso/corp-ldap/login", follow_redirects=False).status_code == 400


def test_oidc_site_login_provisions_and_admin_groups(client, anon, idp):  # noqa: F811
    client.put("/api/sso/providers/okta", json={"config": {"admin_groups": ["finance"]}})
    methods = anon.get("/auth/methods").json()
    assert methods["redirect"][0]["id"] == "okta"
    r = anon.get("/sso/okta/login", params={"next": "/#documents"}, follow_redirects=False)
    q = {k: v[0] for k, v in parse_qs(urlparse(r.headers["location"]).query).items()}
    idp.codes["c"] = q
    r = anon.get("/sso/okta/callback", params={"code": "c", "state": q["state"]}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/#documents"
    me = anon.get("/auth/me").json()
    assert me["username"] == "ann@x.org" and me["provider"] == "okta" and me["is_admin"] is True
    assert anon.get("/api/pipelines").status_code == 200  # same cookie works for the admin API
    # IdP bearer tokens resolve to the provisioned account
    tok = idp.token({"sub": "u-1", "groups": ["eu"]})
    me2 = client.get("/auth/me", headers={"Authorization": f"Bearer {tok}"}).json()
    assert me2["user_id"] == me["user_id"] and me2["groups"] == ["eu"]


def test_saml_login_provisions_account(client, anon, monkeypatch):
    import base64
    import re
    import zlib

    pytest.importorskip("onelogin.saml2.auth")
    from cryptography.hazmat.primitives import serialization

    from dah import config

    from .test_sso import _saml_response, rsa_key, self_signed

    monkeypatch.setenv("DAH_PUBLIC_BASE_URL", "http://dah.example.com")
    config.get_settings.cache_clear()
    key = rsa_key()
    cert = self_signed(key).public_bytes(serialization.Encoding.PEM).decode()
    client.post("/api/sso/providers", json={"id": "adfs", "kind": "saml", "display_name": "ADFS", "config": {
        "idp_entity_id": "https://adfs.example.com/adfs", "idp_sso_url": "https://adfs.example.com/adfs/ls", "idp_x509_cert": cert}})
    r = anon.get("/sso/adfs/login", follow_redirects=False)
    req = zlib.decompress(base64.b64decode(parse_qs(urlparse(r.headers["location"]).query)["SAMLRequest"][0]), -15).decode()
    rid = re.search(r'ID="([^"]+)"', req).group(1)
    resp = _saml_response(key, cert, acs="http://dah.example.com/sso/adfs/acs", sp_entity="http://dah.example.com/sso/adfs/metadata",
                          idp_entity="https://adfs.example.com/adfs", in_response_to=rid, email="cy@x.org", groups=("legal",))
    assert anon.post("/sso/adfs/acs", data={"SAMLResponse": resp}, follow_redirects=False).status_code == 200
    me = anon.get("/auth/me").json()
    assert me["username"] == "cy@x.org" and me["provider"] == "adfs" and me["groups"] == ["legal"]
