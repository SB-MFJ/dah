from __future__ import annotations

import base64
import datetime as dt
import hashlib
import json
import time
from urllib.parse import parse_qs, urlparse

import httpx
import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

ISSUER = "https://idp.example.com"
CLIENT_ID = "dah-client"

SALES_SQL = (
    "SELECT * FROM (VALUES ('eu', 'ann@x.org', 100, 5000), ('us', 'bob@x.org', 200, 6000), ('apac', 'cy@x.org', 300, 7000)) "
    "AS t(region, owner, amount, salary)"
)


# --------------------------------------------------------------------------- helpers


def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def self_signed(key, cn="idp.example.com"):
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = dt.datetime.now(dt.timezone.utc)
    return (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
            .not_valid_after(now + dt.timedelta(days=30)).sign(key, hashes.SHA256()))


def session_for(**kw):
    from dah.sso.core import Principal, issue_session

    kw.setdefault("subject", kw.get("email", "x"))
    kw.setdefault("provider", "okta")
    return issue_session(Principal(**kw))


@pytest.fixture()
def sales(client):
    client.post("/api/pipelines", json={"name": "sales", "kind": "sql", "output_table": "sales", "code": SALES_SQL})
    assert client.post("/api/pipelines/sales/run", json={}).json()["status"] == "succeeded"
    r = client.post("/api/data-endpoints", json={
        "slug": "sales", "source_table": "sales", "description": "regional sales",
        "policy": {
            "providers": ["okta", "corp-saml"],
            "allowed_groups": ["finance"],
            "row_filters": [{"column": "region", "claim": "groups", "op": "in"}],
            "unrestricted_groups": ["auditors"],
            "column_groups": {"salary": ["hr"]},
            "max_rows": 100,
        },
    })
    assert r.status_code == 201, r.text
    return r.json()


# --------------------------------------------------------------------------- ACL / data API


def test_data_endpoint_row_and_column_security(client, anon, sales):
    assert anon.get("/data/sales").status_code == 401

    eu = {"Authorization": "Bearer " + session_for(email="ann@x.org", groups=["finance", "eu"])}
    r = anon.get("/data/sales", headers=eu).json()
    assert r["columns"] == ["region", "owner", "amount"]
    assert r["rows"] == [{"region": "eu", "owner": "ann@x.org", "amount": 100}]

    hr = {"Authorization": "Bearer " + session_for(email="h@x.org", groups=["finance", "hr", "us", "apac"])}
    rows = anon.get("/data/sales", headers=hr, params={"order_by": "-amount"}).json()["rows"]
    assert [r["region"] for r in rows] == ["apac", "us"] and rows[0]["salary"] == 7000

    auditor = {"Authorization": "Bearer " + session_for(email="a@x.org", groups=["finance", "auditors"])}
    assert len(anon.get("/data/sales", headers=auditor).json()["rows"]) == 3

    other = {"Authorization": "Bearer " + session_for(email="z@x.org", groups=["sales"])}
    assert anon.get("/data/sales", headers=other).status_code == 403
    wrong_idp = {"Authorization": "Bearer " + session_for(email="ann@x.org", groups=["finance", "eu"], provider="github")}
    assert anon.get("/data/sales", headers=wrong_idp).status_code == 403

    # user filters are bound parameters; restricted columns can't be selected / filtered / sorted
    assert anon.get("/data/sales", headers=eu, params={"f.region": "eu' OR '1'='1"}).json()["rows"] == []
    assert anon.get("/data/sales", headers=hr, params={"f.region": "us"}).json()["count"] == 1
    assert anon.get("/data/sales", headers=eu, params={"columns": "salary"}).status_code == 400
    assert anon.get("/data/sales", headers=eu, params={"f.salary": "5000"}).status_code == 400
    assert anon.get("/data/sales", headers=eu, params={"order_by": "salary"}).status_code == 400

    csv_ = anon.get("/data/sales", headers=eu, params={"format": "csv"}).text.splitlines()
    assert csv_ == ["region,owner,amount", "eu,ann@x.org,100"]
    assert anon.get("/data/sales/schema", headers=eu).json()["columns"] == ["region", "owner", "amount"]
    assert [e["slug"] for e in anon.get("/data", headers=eu).json()["endpoints"]] == ["sales"]
    assert anon.get("/data", headers=other).json()["endpoints"] == []

    # tampered / expired session tokens
    tok = session_for(email="ann@x.org", groups=["finance", "eu"])
    assert anon.get("/data/sales", headers={"Authorization": "Bearer " + tok[:-3] + "abc"}).status_code == 401
    from dah.sso.core import Principal, issue_session

    expired = issue_session(Principal(subject="a", provider="okta", groups=["finance", "eu"]), ttl=-10)
    assert anon.get("/data/sales", headers={"Authorization": "Bearer " + expired}).status_code == 401

    log = client.get("/api/access-log", params={"endpoint": "sales"}).json()["items"]
    assert any(not x["allowed"] and x["subject"] == "z@x.org" for x in log)
    assert any(x["allowed"] and x["subject"] == "ann@x.org" and x["rows"] == 1 for x in log)


def test_missing_claim_fails_closed_and_user_allowlist(client, anon, sales):
    client.put("/api/data-endpoints/sales", json={"policy": {
        "allowed_users": ["ann@x.org"], "row_filters": [{"column": "region", "claim": "attributes.region"}]}})
    no_attr = {"Authorization": "Bearer " + session_for(email="ann@x.org")}
    assert anon.get("/data/sales", headers=no_attr).json()["rows"] == []
    with_attr = {"Authorization": "Bearer " + session_for(email="ANN@x.org", attributes={"region": "us"})}
    assert [r["region"] for r in anon.get("/data/sales", headers=with_attr).json()["rows"]] == ["us"]
    bob = {"Authorization": "Bearer " + session_for(email="bob@x.org", attributes={"region": "us"})}
    assert anon.get("/data/sales", headers=bob).status_code == 403


def test_endpoint_admin_validation_and_simulate(client, sales):
    bad = {"slug": "x", "source_table": "sales", "policy": {"row_filters": [{"column": "region; drop", "claim": "email"}]}}
    assert client.post("/api/data-endpoints", json=bad).status_code == 400
    assert client.post("/api/data-endpoints", json={"slug": "y", "source_table": "nope"}).status_code == 400
    assert client.post("/api/data-endpoints", json={"slug": "z", "source_sql": "SELECT 1", "source_table": "sales"}).status_code == 400
    sim = client.post("/api/data-endpoints/sales/simulate", json={"principal": {"email": "a@b", "provider": "okta", "groups": ["finance", "us"]}}).json()
    assert sim["allowed"] and sim["rows"] == [{"region": "us", "owner": "bob@x.org", "amount": 200}] and "$1" in sim["sql"]
    assert client.post("/api/data-endpoints/sales/simulate", json={"principal": {"groups": ["x"], "provider": "okta"}}).json()["allowed"] is False
    # admin-only
    from fastapi.testclient import TestClient

    from dah.api.app import create_app

    with TestClient(create_app()) as anon:
        assert anon.get("/api/data-endpoints").status_code == 401


def test_data_endpoint_over_documents_with_sql_source(client, anon):
    from .test_ingest import upload

    upload(client, "a.txt", b"alpha", metadata={"dept": "legal"})
    upload(client, "b.txt", b"beta", metadata={"dept": "hr"})
    r = client.post("/api/data-endpoints", json={
        "slug": "docs", "source_sql": "SELECT path, text, metadata->'user'->>'dept' AS dept FROM documents",
        "policy": {"row_filters": [{"column": "dept", "claim": "attributes.dept"}]}})
    assert r.status_code == 201, r.text
    h = {"Authorization": "Bearer " + session_for(email="l@x", attributes={"dept": "legal"})}
    assert anon.get("/data/docs", headers=h).json()["rows"] == [{"path": "a.txt", "text": "alpha", "dept": "legal"}]


# --------------------------------------------------------------------------- OIDC


class FakeIdP:
    def __init__(self):
        self.key = rsa_key()
        jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
        self.jwks = {"keys": [{**jwk, "kid": "k1", "use": "sig", "alg": "RS256"}]}
        self.codes: dict[str, dict] = {}

    def token(self, claims, aud=CLIENT_ID, key=None):
        now = int(time.time())
        body = {"iss": ISSUER, "aud": aud, "iat": now, "exp": now + 300, **claims}
        return jwt.encode(body, key or self.key, algorithm="RS256", headers={"kid": "k1"})

    def handler(self, request: httpx.Request) -> httpx.Response:
        p = request.url.path
        if p == "/.well-known/openid-configuration":
            return httpx.Response(200, json={
                "issuer": ISSUER, "authorization_endpoint": f"{ISSUER}/authorize", "token_endpoint": f"{ISSUER}/token",
                "jwks_uri": f"{ISSUER}/jwks", "token_endpoint_auth_methods_supported": ["client_secret_basic"]})
        if p == "/jwks":
            return httpx.Response(200, json=self.jwks)
        if p == "/token":
            form = parse_qs(request.content.decode())
            code = form["code"][0]
            pending = self.codes.pop(code, None)
            if pending is None:
                return httpx.Response(400, json={"error": "invalid_grant"})
            challenge = base64.urlsafe_b64encode(hashlib.sha256(form["code_verifier"][0].encode()).digest()).rstrip(b"=").decode()
            assert challenge == pending["code_challenge"], "PKCE mismatch"
            assert request.headers["authorization"] == "Basic " + base64.b64encode(f"{CLIENT_ID}:s3cret".encode()).decode()
            return httpx.Response(200, json={"access_token": "at", "token_type": "Bearer",
                                             "id_token": self.token({"sub": "u-1", "email": "ann@x.org", "name": "Ann", "groups": ["finance", "eu"],
                                                                     "department": "fin", "nonce": pending["nonce"]})})
        return httpx.Response(404)


@pytest.fixture()
def idp(client, monkeypatch):
    from dah.sso import oidc

    fake = FakeIdP()
    monkeypatch.setattr(oidc, "http", httpx.Client(transport=httpx.MockTransport(fake.handler)))
    oidc._discovery.clear()
    oidc._jwks.clear()
    r = client.post("/api/sso/providers", json={"id": "okta", "kind": "oidc", "display_name": "Okta", "config": {
        "issuer": ISSUER, "client_id": CLIENT_ID, "client_secret": "s3cret", "attribute_claims": ["department"]}})
    assert r.status_code == 201, r.text
    assert r.json()["config"]["client_secret"] == {"set": True}
    return fake


def test_oidc_login_flow(client, anon, idp, sales):
    r = anon.get("/sso/okta/login", params={"next": "/data/sales"}, follow_redirects=False)
    assert r.status_code == 302
    q = {k: v[0] for k, v in parse_qs(urlparse(r.headers["location"]).query).items()}
    assert q["client_id"] == CLIENT_ID and q["code_challenge_method"] == "S256" and q["redirect_uri"] == "http://testserver/sso/okta/callback"
    idp.codes["the-code"] = q

    bad = anon.get("/sso/okta/callback", params={"code": "the-code", "state": "forged"}, follow_redirects=False)
    assert bad.status_code == 400

    r = anon.get("/sso/okta/callback", params={"code": "the-code", "state": q["state"]}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/data/sales"
    me = anon.get("/sso/me").json()
    assert me["email"] == "ann@x.org" and me["groups"] == ["finance", "eu"] and me["attributes"] == {"department": "fin"}
    assert anon.get("/data/sales").json()["rows"] == [{"region": "eu", "owner": "ann@x.org", "amount": 100}]
    anon.post("/sso/logout")
    anon.cookies.clear()
    assert anon.get("/data/sales").status_code == 401


def test_oidc_open_redirect_blocked(anon, idp):
    r = anon.get("/sso/okta/login", params={"next": "https://evil.example/"}, follow_redirects=False)
    q = {k: v[0] for k, v in parse_qs(urlparse(r.headers["location"]).query).items()}
    idp.codes["c2"] = q
    r = anon.get("/sso/okta/callback", params={"code": "c2", "state": q["state"]}, follow_redirects=False)
    assert r.status_code == 200 and "Signed in" in r.text and "evil" not in r.headers.get("location", "")


def test_oidc_bearer_tokens(anon, idp, sales):
    good = idp.token({"sub": "svc-1", "email": "svc@x.org", "groups": ["finance", "us"]})
    assert anon.get("/data/sales", headers={"Authorization": f"Bearer {good}"}).json()["rows"][0]["region"] == "us"
    wrong_aud = idp.token({"sub": "s", "groups": ["finance", "us"]}, aud="someone-else")
    assert anon.get("/data/sales", headers={"Authorization": f"Bearer {wrong_aud}"}).status_code == 401
    forged = idp.token({"sub": "s", "groups": ["finance", "us"]}, key=rsa_key())
    assert anon.get("/data/sales", headers={"Authorization": f"Bearer {forged}"}).status_code == 401
    unknown_iss = jwt.encode({"iss": "https://other", "sub": "x", "exp": int(time.time()) + 60}, idp.key, algorithm="RS256")
    assert anon.get("/data/sales", headers={"Authorization": f"Bearer {unknown_iss}"}).status_code == 401


# --------------------------------------------------------------------------- SAML


def _saml_response(key, cert_pem, *, acs, sp_entity, idp_entity, in_response_to, email="ann@x.org", groups=("finance", "eu")):
    from onelogin.saml2.constants import OneLogin_Saml2_Constants as C
    from onelogin.saml2.utils import OneLogin_Saml2_Utils as U

    now = dt.datetime.now(dt.timezone.utc)
    fmt = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731
    before, later = fmt(now - dt.timedelta(minutes=2)), fmt(now + dt.timedelta(minutes=5))
    attrs = "".join(f"<saml:AttributeValue>{g}</saml:AttributeValue>" for g in groups)
    assertion = f"""<saml:Assertion xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" ID="_a{int(time.time()*1000)}" Version="2.0" IssueInstant="{fmt(now)}">
<saml:Issuer>{idp_entity}</saml:Issuer>
<saml:Subject><saml:NameID Format="urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress">{email}</saml:NameID>
<saml:SubjectConfirmation Method="urn:oasis:names:tc:SAML:2.0:cm:bearer"><saml:SubjectConfirmationData NotOnOrAfter="{later}" Recipient="{acs}" InResponseTo="{in_response_to}"/></saml:SubjectConfirmation></saml:Subject>
<saml:Conditions NotBefore="{before}" NotOnOrAfter="{later}"><saml:AudienceRestriction><saml:Audience>{sp_entity}</saml:Audience></saml:AudienceRestriction></saml:Conditions>
<saml:AuthnStatement AuthnInstant="{fmt(now)}" SessionIndex="_s1"><saml:AuthnContext><saml:AuthnContextClassRef>urn:oasis:names:tc:SAML:2.0:ac:classes:Password</saml:AuthnContextClassRef></saml:AuthnContext></saml:AuthnStatement>
<saml:AttributeStatement><saml:Attribute Name="groups">{attrs}</saml:Attribute><saml:Attribute Name="email"><saml:AttributeValue>{email}</saml:AttributeValue></saml:Attribute></saml:AttributeStatement>
</saml:Assertion>"""
    key_pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    signed = U.add_sign(assertion, key_pem, cert_pem, sign_algorithm=C.RSA_SHA256, digest_algorithm=C.SHA256)
    signed = signed.decode() if isinstance(signed, bytes) else signed
    signed = signed.split("?>", 1)[-1] if signed.startswith("<?xml") else signed
    resp = (f'<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" xmlns:saml="urn:oasis:names:tc:SAML:2.0:assertion" '
            f'ID="_r{int(time.time()*1000)}" Version="2.0" IssueInstant="{fmt(now)}" Destination="{acs}" InResponseTo="{in_response_to}">'
            f'<saml:Issuer>{idp_entity}</saml:Issuer><samlp:Status><samlp:StatusCode Value="urn:oasis:names:tc:SAML:2.0:status:Success"/></samlp:Status>'
            f"{signed}</samlp:Response>")
    return base64.b64encode(resp.encode()).decode()


BASE = "http://dah.example.com"


@pytest.fixture()
def saml_idp(client, monkeypatch):
    pytest.importorskip("onelogin.saml2.auth")
    # python3-saml requires ACS URLs with a real domain; the public base URL drives SAML URLs.
    from dah import config

    monkeypatch.setenv("DAH_PUBLIC_BASE_URL", BASE)
    config.get_settings.cache_clear()
    key = rsa_key()
    cert_pem = self_signed(key).public_bytes(serialization.Encoding.PEM).decode()
    r = client.post("/api/sso/providers", json={"id": "corp-saml", "kind": "saml", "display_name": "Corp ADFS", "config": {
        "idp_entity_id": "https://adfs.example.com/adfs", "idp_sso_url": "https://adfs.example.com/adfs/ls", "idp_x509_cert": cert_pem}})
    assert r.status_code == 201, r.text
    return key, cert_pem


def test_saml_metadata_login_and_acs(client, anon, saml_idp, sales):
    key, cert_pem = saml_idp
    md = anon.get("/sso/corp-saml/metadata")
    assert md.status_code == 200 and f"{BASE}/sso/corp-saml/acs" in md.text and "EntityDescriptor" in md.text

    r = anon.get("/sso/corp-saml/login", params={"next": "/data/sales"}, follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("https://adfs.example.com/adfs/ls?SAMLRequest=")
    import zlib

    req_xml = zlib.decompress(base64.b64decode(parse_qs(urlparse(r.headers["location"]).query)["SAMLRequest"][0]), -15).decode()
    import re

    req_id = re.search(r'ID="([^"]+)"', req_xml).group(1)
    acs, sp_entity = f"{BASE}/sso/corp-saml/acs", f"{BASE}/sso/corp-saml/metadata"
    good = _saml_response(key, cert_pem, acs=acs, sp_entity=sp_entity, idp_entity="https://adfs.example.com/adfs", in_response_to=req_id)

    r = anon.post("/sso/corp-saml/acs", data={"SAMLResponse": good, "RelayState": "/data/sales"}, follow_redirects=False)
    assert r.status_code == 303, r.text
    assert r.headers["location"] == "/data/sales"
    me = anon.get("/sso/me").json()
    assert me["provider"] == "corp-saml" and me["email"] == "ann@x.org" and me["groups"] == ["finance", "eu"]
    assert [x["region"] for x in anon.get("/data/sales").json()["rows"]] == ["eu"]

    # replaying the same response fails (request id consumed)
    anon.cookies.clear()
    assert anon.post("/sso/corp-saml/acs", data={"SAMLResponse": good}, follow_redirects=False).status_code == 401
    # unsolicited response fails
    unsolicited = _saml_response(key, cert_pem, acs=acs, sp_entity=sp_entity, idp_entity="https://adfs.example.com/adfs", in_response_to="_nope")
    assert anon.post("/sso/corp-saml/acs", data={"SAMLResponse": unsolicited}, follow_redirects=False).status_code == 401


def test_saml_rejects_wrong_signer(client, anon, saml_idp):
    _, cert_pem = saml_idp
    r = anon.get("/sso/corp-saml/login", follow_redirects=False)
    import re
    import zlib

    req_xml = zlib.decompress(base64.b64decode(parse_qs(urlparse(r.headers["location"]).query)["SAMLRequest"][0]), -15).decode()
    req_id = re.search(r'ID="([^"]+)"', req_xml).group(1)
    other = rsa_key()
    other_cert = self_signed(other, "evil").public_bytes(serialization.Encoding.PEM).decode()
    forged = _saml_response(other, other_cert, acs=f"{BASE}/sso/corp-saml/acs", sp_entity=f"{BASE}/sso/corp-saml/metadata",
                            idp_entity="https://adfs.example.com/adfs", in_response_to=req_id)
    r = anon.post("/sso/corp-saml/acs", data={"SAMLResponse": forged}, follow_redirects=False)
    assert r.status_code == 401 and "rejected" in r.json()["detail"]


def test_multiple_providers_listed(client, anon, idp, saml_idp):
    ids = [p["id"] for p in anon.get("/sso/providers").json()["items"]]
    assert ids == ["corp-saml", "okta"]
    page = anon.get("/sso").text
    assert "Okta" in page and "Corp ADFS" in page
    client.put("/api/sso/providers/okta", json={"enabled": False})
    assert [p["id"] for p in anon.get("/sso/providers").json()["items"]] == ["corp-saml"]
    assert anon.get("/sso/okta/login", follow_redirects=False).status_code == 404
    # secret preserved when updating other config fields
    client.put("/api/sso/providers/okta", json={"enabled": True, "config": {"scopes": ["openid", "email"]}})
    from dah.db import IdentityProvider, session_scope
    from dah.sso.core import load_config

    with session_scope() as s:
        cfg = load_config(s.get(IdentityProvider, "okta"))
    assert cfg["client_secret"] == "s3cret" and cfg["scopes"] == ["openid", "email"]
