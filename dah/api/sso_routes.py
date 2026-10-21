"""SSO login flows, the access-controlled /data API, and admin endpoints to configure both."""

from __future__ import annotations

import base64
import csv
import html
import io
import json
import re
import time
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import lake
from ..config import get_settings
from ..db import AccessLog, DataEndpoint, IdentityProvider, Setting, dumps, session_scope
from ..sso import acl, core, oidc, saml
from ..auth.identity import AuthError, Identity, identity_for_user, issue_user_session, provision_external
from .deps import DB, Admin, current_identity

admin = APIRouter(prefix="/api", tags=["sso-admin"], dependencies=[Admin])
public = APIRouter(tags=["sso"])

SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
FLOW_COOKIE = "dah_sso_flow"


# =========================================================================== admin: providers


class ProviderIn(BaseModel):
    id: str
    kind: Literal["oidc", "saml", "ldap"]
    display_name: str
    enabled: bool = True
    config: dict = Field(default_factory=dict)


class ProviderUpdate(BaseModel):
    display_name: str | None = None
    enabled: bool | None = None
    config: dict | None = None


def _check_provider_config(kind: str, cfg: dict) -> None:
    need = {"oidc": ("issuer", "client_id"), "saml": ("idp_entity_id", "idp_sso_url", "idp_x509_cert"),
            "ldap": ("server_url", "user_search_base")}[kind]
    missing = [k for k in need if not cfg.get(k)]
    if missing:
        raise HTTPException(400, f"missing {kind} config: {missing}")


@admin.get("/sso/providers")
def list_providers(db: Session = DB):
    return {"items": [core.provider_dict(p) for p in db.scalars(select(IdentityProvider).order_by(IdentityProvider.id))]}


@admin.post("/sso/providers", status_code=201)
def create_provider(body: ProviderIn, db: Session = DB):
    if not SLUG.match(body.id):
        raise HTTPException(400, "id must be a lowercase slug")
    if db.get(IdentityProvider, body.id):
        raise HTTPException(409, "provider exists")
    cfg = {**core.defaults_for(body.kind), **body.config}
    _check_provider_config(body.kind, cfg)
    p = IdentityProvider(id=body.id, kind=body.kind, display_name=body.display_name, enabled=body.enabled, config_json=core.store_config(body.kind, cfg))
    db.add(p)
    db.flush()
    return core.provider_dict(p)


@admin.get("/sso/providers/{provider_id}")
def get_provider(provider_id: str, db: Session = DB):
    p = db.get(IdentityProvider, provider_id)
    if p is None:
        raise HTTPException(404, "provider not found")
    return core.provider_dict(p)


@admin.put("/sso/providers/{provider_id}")
def update_provider(provider_id: str, body: ProviderUpdate, db: Session = DB):
    p = db.get(IdentityProvider, provider_id)
    if p is None:
        raise HTTPException(404, "provider not found")
    if body.display_name is not None:
        p.display_name = body.display_name
    if body.enabled is not None:
        p.enabled = body.enabled
    if body.config is not None:
        prev = json.loads(p.config_json or "{}")
        cfg = {**core.defaults_for(p.kind), **{k: v for k, v in prev.items() if k not in core.SECRET_KEYS}, **body.config}
        _check_provider_config(p.kind, cfg)
        p.config_json = core.store_config(p.kind, cfg, previous=prev)
    return core.provider_dict(p)


@admin.post("/sso/providers/{provider_id}/test")
def test_provider(provider_id: str, db: Session = DB):
    """Connectivity check: LDAP service bind + search base, or OIDC discovery."""
    p = db.get(IdentityProvider, provider_id)
    if p is None:
        raise HTTPException(404, "provider not found")
    cfg = core.load_config(p)
    if p.kind == "ldap":
        from ..auth import ldap

        return ldap.test_connection(cfg)
    if p.kind == "oidc":
        try:
            return {"ok": True, "issuer": oidc.discovery(cfg).get("issuer")}
        except oidc.OIDCError as e:
            return {"ok": False, "error": str(e)}
    try:
        saml.metadata_xml(p.id, cfg)
        return {"ok": True}
    except saml.SAMLError as e:
        return {"ok": False, "error": str(e)}


@admin.delete("/sso/providers/{provider_id}")
def delete_provider(provider_id: str, db: Session = DB):
    p = db.get(IdentityProvider, provider_id)
    if p is None:
        raise HTTPException(404, "provider not found")
    db.delete(p)
    return {"deleted": provider_id}


# =========================================================================== admin: data endpoints


class EndpointIn(BaseModel):
    slug: str
    description: str = ""
    source_table: str | None = None
    source_sql: str | None = None
    policy: dict = Field(default_factory=dict)
    enabled: bool = True


class EndpointUpdate(BaseModel):
    description: str | None = None
    source_table: str | None = None
    source_sql: str | None = None
    policy: dict | None = None
    enabled: bool | None = None


class SimulateIn(BaseModel):
    principal: dict
    columns: list[str] | None = None
    filters: dict[str, str] | None = None
    limit: int = 20


def endpoint_dict(e: DataEndpoint) -> dict:
    base = get_settings().public_base_url.rstrip("/")
    return {"slug": e.slug, "description": e.description, "source_table": e.source_table, "source_sql": e.source_sql,
            "policy": e.policy, "enabled": e.enabled, "url": f"{base}/data/{e.slug}", "created_at": e.created_at, "updated_at": e.updated_at}


def _check_endpoint(source_table: str | None, source_sql: str | None, policy: dict) -> None:
    if bool(source_table) == bool(source_sql):
        raise HTTPException(400, "set exactly one of source_table or source_sql")
    if source_table and not acl.IDENT.match(source_table):
        raise HTTPException(400, "source_table must be an identifier")
    try:
        acl.validate_policy(policy)
    except acl.PolicyError as e:
        raise HTTPException(400, str(e))
    _source_columns(source_table, source_sql)  # must bind


@admin.get("/data-endpoints")
def list_endpoints(db: Session = DB):
    return {"items": [endpoint_dict(e) for e in db.scalars(select(DataEndpoint).order_by(DataEndpoint.slug))]}


@admin.post("/data-endpoints", status_code=201)
def create_endpoint(body: EndpointIn, db: Session = DB):
    if not SLUG.match(body.slug):
        raise HTTPException(400, "slug must be lowercase letters, digits, - or _")
    if db.get(DataEndpoint, body.slug):
        raise HTTPException(409, "endpoint exists")
    _check_endpoint(body.source_table, body.source_sql, body.policy)
    e = DataEndpoint(slug=body.slug, description=body.description, source_table=body.source_table or None,
                     source_sql=body.source_sql or None, policy_json=dumps(body.policy), enabled=body.enabled)
    db.add(e)
    db.flush()
    return endpoint_dict(e)


@admin.get("/data-endpoints/{slug}")
def get_endpoint(slug: str, db: Session = DB):
    e = db.get(DataEndpoint, slug)
    if e is None:
        raise HTTPException(404, "endpoint not found")
    return endpoint_dict(e)


@admin.put("/data-endpoints/{slug}")
def update_endpoint(slug: str, body: EndpointUpdate, db: Session = DB):
    e = db.get(DataEndpoint, slug)
    if e is None:
        raise HTTPException(404, "endpoint not found")
    ch = body.model_dump(exclude_unset=True)
    src_t = ch.get("source_table", e.source_table) or None
    src_s = ch.get("source_sql", e.source_sql) or None
    pol = ch.get("policy", e.policy)
    _check_endpoint(src_t, src_s, pol)
    e.source_table, e.source_sql, e.policy_json = src_t, src_s, dumps(pol)
    if "description" in ch:
        e.description = ch["description"] or ""
    if "enabled" in ch:
        e.enabled = bool(ch["enabled"])
    return endpoint_dict(e)


@admin.delete("/data-endpoints/{slug}")
def delete_endpoint(slug: str, db: Session = DB):
    e = db.get(DataEndpoint, slug)
    if e is None:
        raise HTTPException(404, "endpoint not found")
    db.delete(e)
    return {"deleted": slug}


@admin.post("/data-endpoints/{slug}/simulate")
def simulate(slug: str, body: SimulateIn, db: Session = DB):
    """Evaluate the policy for a hypothetical principal: decision, generated SQL and a row preview."""
    e = db.get(DataEndpoint, slug)
    if e is None:
        raise HTTPException(404, "endpoint not found")
    pr = body.principal
    who = core.Principal(subject=pr.get("subject") or pr.get("email") or "simulated", provider=pr.get("provider", ""),
                         email=pr.get("email"), name=pr.get("name"), groups=core.as_list(pr.get("groups")), attributes=pr.get("attributes") or {})
    ok, reason = acl.authorize(e.policy, who)
    out = {"allowed": ok, "reason": reason}
    if ok:
        res = _read(e, who, columns=body.columns, filters=body.filters, order_by=None, limit=body.limit, offset=0)
        out.update(sql=res.pop("_sql"), params=res.pop("_params"), **res)
    return out


@admin.get("/access-log")
def access_log(endpoint: str | None = None, limit: int = Query(200, le=5000), db: Session = DB):
    q = select(AccessLog).order_by(AccessLog.id.desc()).limit(limit)
    if endpoint:
        q = q.where(AccessLog.endpoint == endpoint)
    return {"items": [{"id": a.id, "endpoint": a.endpoint, "subject": a.subject, "provider": a.provider, "allowed": a.allowed,
                       "rows": a.rows, "reason": a.reason, "at": a.at} for a in db.scalars(q)]}


# =========================================================================== principal resolution


def require_principal(ident: Identity = Depends(current_identity)) -> core.Principal:
    """The caller as an access-policy principal (accounts carry their merged local + IdP groups)."""
    return ident.principal()


# =========================================================================== /data


def _source_sql(e_table: str | None, e_sql: str | None) -> str:
    return f'SELECT * FROM "{e_table}"' if e_table else e_sql.strip().rstrip(";")


def _source_columns(source_table: str | None, source_sql: str | None, con=None) -> list[str]:
    own = con is None
    con = con or lake.connect()
    try:
        rows = con.execute(f"DESCRIBE {_source_sql(source_table, source_sql)}").fetchall()
        return [r[0] for r in rows]
    except Exception as ex:
        raise HTTPException(400, f"endpoint source does not bind: {ex}")
    finally:
        if own:
            con.close()


def _read(e: DataEndpoint, who: core.Principal, *, columns, filters, order_by, limit, offset) -> dict:
    con = lake.connect()
    try:
        available = _source_columns(e.source_table, e.source_sql, con)
        try:
            sql, params, selected = acl.build_query(_source_sql(e.source_table, e.source_sql), available, e.policy, who,
                                                    columns=columns, user_filters=filters, order_by=order_by, limit=limit, offset=offset)
        except acl.PolicyError as ex:
            raise HTTPException(400, str(ex))
        res = lake.run_query(sql, params, limit=None, con=con)
    finally:
        con.close()
    return {"columns": selected, "rows": res["rows"], "count": len(res["rows"]), "offset": offset, "_sql": sql, "_params": params}


def _log(endpoint: str, who: core.Principal | None, allowed: bool, rows: int, reason: str) -> None:
    with session_scope() as s:
        s.add(AccessLog(endpoint=endpoint, subject=(who.email or who.subject) if who else "anonymous",
                        provider=who.provider if who else "", allowed=allowed, rows=rows, reason=reason[:500]))


@public.get("/data")
def data_index(who: core.Principal = Depends(require_principal), db: Session = DB):
    """Endpoints the caller may read."""
    items = []
    for e in db.scalars(select(DataEndpoint).where(DataEndpoint.enabled.is_(True)).order_by(DataEndpoint.slug)):
        if acl.authorize(e.policy, who)[0]:
            items.append({"slug": e.slug, "description": e.description, "url": f"/data/{e.slug}"})
    return {"principal": who.to_dict(), "endpoints": items}


def _endpoint_for(slug: str, who: core.Principal, db: Session) -> DataEndpoint:
    e = db.get(DataEndpoint, slug)
    if e is None or not e.enabled:
        _log(slug, who, False, 0, "unknown endpoint")
        raise HTTPException(404, "endpoint not found")
    ok, reason = acl.authorize(e.policy, who)
    if not ok:
        _log(slug, who, False, 0, reason)
        raise HTTPException(403, f"access denied: {reason}")
    return e


@public.get("/data/{slug}/schema")
def data_schema(slug: str, who: core.Principal = Depends(require_principal), db: Session = DB):
    e = _endpoint_for(slug, who, db)
    available = _source_columns(e.source_table, e.source_sql)
    return {"slug": slug, "columns": acl.visible_columns(e.policy, who, available), "max_rows": int(e.policy.get("max_rows") or 10000)}


@public.get("/data/{slug}")
def data_read(
    slug: str,
    request: Request,
    columns: str | None = Query(None, description="comma-separated subset of visible columns"),
    order_by: str | None = Query(None, description="column, prefix with - for descending"),
    limit: int = Query(1000, ge=1),
    offset: int = Query(0, ge=0),
    format: Literal["json", "csv", "ndjson"] = "json",
    who: core.Principal = Depends(require_principal),
    db: Session = DB,
):
    """Read rows the caller is allowed to see. Equality filters: ``?f.<column>=<value>``."""
    e = _endpoint_for(slug, who, db)
    filters = {k[2:]: v for k, v in request.query_params.items() if k.startswith("f.")}
    cols = [c.strip() for c in columns.split(",") if c.strip()] if columns else None
    try:
        res = _read(e, who, columns=cols, filters=filters, order_by=order_by, limit=limit, offset=offset)
    except HTTPException as ex:
        _log(slug, who, False, 0, str(ex.detail))
        raise
    res.pop("_sql"), res.pop("_params")
    _log(slug, who, True, res["count"], "ok")
    if format == "csv":
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(res["columns"])
        for r in res["rows"]:
            w.writerow([json.dumps(r[c]) if isinstance(r[c], (dict, list)) else r[c] for c in res["columns"]])
        return Response(buf.getvalue(), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{slug}.csv"'})
    if format == "ndjson":
        return Response("".join(json.dumps(r) + "\n" for r in res["rows"]), media_type="application/x-ndjson")
    nxt = offset + res["count"] if res["count"] == min(limit, int(e.policy.get("max_rows") or 10000)) else None
    return {"endpoint": slug, **res, "next_offset": nxt}


# =========================================================================== login flows


def _cookie_secure() -> bool:
    return get_settings().public_base_url.startswith("https://")


def _login_response(db: Session, cfg: dict, who: core.Principal, next_url: str | None) -> Response:
    """Provision / refresh the user account for this IdP identity and start a session."""
    try:
        user = provision_external(db, who, cfg.get("admin_groups"))
    except AuthError as e:
        raise HTTPException(e.status, str(e))
    token = issue_user_session(user)
    who = identity_for_user(user, "cookie").principal()
    if next_url:
        resp: Response = RedirectResponse(next_url, status_code=303)
    else:
        resp = HTMLResponse(_done_page(who, token))
    resp.set_cookie(core.SESSION_COOKIE, token, max_age=get_settings().session_ttl_s, httponly=True,
                    secure=_cookie_secure(), samesite="lax", path="/")
    resp.delete_cookie(FLOW_COOKIE, path="/sso")
    return resp


def _page(title: str, body: str) -> str:
    return (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
            f"<title>{html.escape(title)}</title><style>body{{font:15px system-ui,sans-serif;max-width:680px;margin:48px auto;padding:0 16px;color:#1d2330}}"
            f"code,textarea{{font:13px ui-monospace,Menlo,monospace}}textarea{{width:100%;height:110px}}a.btn{{display:block;padding:10px 14px;margin:8px 0;"
            f"border:1px solid #d0d5dd;border-radius:8px;text-decoration:none;color:inherit}}a.btn:hover{{border-color:#2f6fed}}</style>"
            f"<h1>{html.escape(title)}</h1>{body}")


def _done_page(who: core.Principal, token: str) -> str:
    groups = ", ".join(who.groups) or "none"
    return _page("Signed in", (
        f"<p>Signed in as <b>{html.escape(who.email or who.subject)}</b> via <code>{html.escape(who.provider)}</code>. Groups: {html.escape(groups)}.</p>"
        f"<p>Your browser now has a session cookie. For API clients use this bearer token "
        f"(valid {get_settings().session_ttl_s // 3600} h):</p><textarea readonly>{html.escape(token)}</textarea>"
        f"<p><code>curl -H 'Authorization: Bearer &lt;token&gt;' {html.escape(get_settings().public_base_url.rstrip('/'))}/data</code></p>"
        f"<p><a href='/data'>Endpoints available to you</a></p>"))


@public.get("/sso", response_class=HTMLResponse)
def login_page(next: str | None = None, error: str | None = None, db: Session = DB):
    """Standalone sign-in page (for /data users and API clients): redirect providers + password form."""
    nxt = core.safe_next(next)
    provs = list(db.scalars(select(IdentityProvider).where(IdentityProvider.enabled.is_(True)).order_by(IdentityProvider.display_name)))
    links = "".join(
        f"<a class=btn href='/sso/{html.escape(p.id)}/login{('?next=' + html.escape(nxt)) if nxt else ''}'>{html.escape(p.display_name)} "
        f"<small>({p.kind.upper()})</small></a>"
        for p in provs if p.kind != "ldap"
    )
    pw = ([("local", "Local account")] if get_settings().local_login_enabled else []) + [(p.id, p.display_name) for p in provs if p.kind == "ldap"]
    form = ""
    if pw:
        options = "".join(f"<option value='{html.escape(i)}'>{html.escape(n)}</option>" for i, n in pw)
        form = ("<form method=post action='/auth/login' style='display:grid;gap:8px;margin-top:16px'>"
                f"<select name=provider>{options}</select><input name=username placeholder=Username autocomplete=username required>"
                "<input name=password type=password placeholder=Password autocomplete=current-password required>"
                f"<input type=hidden name=next value='{html.escape(nxt or '/sso/me')}'><button>Sign in</button></form>")
    msg = {"invalid": "Invalid username or password.", "locked": "Too many attempts; try again later."}.get(error or "", "")
    err = f"<p style='color:#c4320a'>{msg}</p>" if msg else ""
    return _page("Sign in", err + (links + form or "<p>No sign-in methods are configured.</p>"))


@public.get("/sso/providers")
def public_providers(db: Session = DB):
    return {"items": [{"id": p.id, "kind": p.kind, "display_name": p.display_name,
                       "login_url": "/auth/login" if p.kind == "ldap" else f"/sso/{p.id}/login"}
                      for p in db.scalars(select(IdentityProvider).where(IdentityProvider.enabled.is_(True)).order_by(IdentityProvider.id))]}


def _provider_or_404(db: Session, provider_id: str) -> tuple[IdentityProvider, dict]:
    p = core.get_provider(db, provider_id)
    if p is None:
        raise HTTPException(404, "identity provider not found")
    return p, core.load_config(p)


_SAML_PENDING = "saml.pending."


def _saml_remember(request_id: str, next_url: str | None) -> None:
    with session_scope() as s:
        now = time.time()
        for old in s.scalars(select(Setting).where(Setting.key.startswith(_SAML_PENDING))):
            if json.loads(old.value).get("exp", 0) < now:
                s.delete(old)
        s.add(Setting(key=_SAML_PENDING + request_id, value=json.dumps({"next": next_url, "exp": now + 600})))


def _saml_consume(request_id: str | None) -> dict | None:
    if not request_id:
        return None
    with session_scope() as s:
        row = s.get(Setting, _SAML_PENDING + request_id)
        if row is None:
            return None
        data = json.loads(row.value)
        s.delete(row)
    return data if data.get("exp", 0) >= time.time() else None


@public.get("/sso/{provider_id}/login")
def sso_login(provider_id: str, next: str | None = None, db: Session = DB):
    p, cfg = _provider_or_404(db, provider_id)
    if p.kind == "ldap":
        raise HTTPException(400, "LDAP providers use the password form: POST /auth/login")
    nxt = core.safe_next(next)
    try:
        if p.kind == "oidc":
            url, flow = oidc.begin_login(p.id, cfg)
            resp = RedirectResponse(url, status_code=302)
            resp.set_cookie(FLOW_COOKIE, core.sign_flow({**flow, "idp": p.id, "next": nxt}), max_age=600, httponly=True,
                            secure=_cookie_secure(), samesite="lax", path="/sso")
            return resp
        url, request_id = saml.begin_login(p.id, cfg, relay_state=nxt or "")
        _saml_remember(request_id, nxt)
        return RedirectResponse(url, status_code=302)
    except (oidc.OIDCError, saml.SAMLError) as e:
        raise HTTPException(502, str(e))


@public.get("/sso/{provider_id}/callback")
def oidc_callback(provider_id: str, request: Request, code: str | None = None, state: str | None = None,
                  error: str | None = None, error_description: str | None = None, db: Session = DB):
    p, cfg = _provider_or_404(db, provider_id)
    if p.kind != "oidc":
        raise HTTPException(404, "not an OIDC provider")
    if error:
        raise HTTPException(401, f"identity provider error: {error} {error_description or ''}".strip())
    flow = core.verify_flow(request.cookies.get(FLOW_COOKIE))
    if not flow or flow.get("idp") != p.id or not state or state != flow.get("state") or not code:
        raise HTTPException(400, "invalid or expired login state; start again")
    try:
        who = oidc.finish_login(p.id, cfg, code, flow)
    except oidc.OIDCError as e:
        raise HTTPException(401, str(e))
    return _login_response(db, cfg, who, core.safe_next(flow.get("next")))


def _in_response_to(saml_response_b64: str) -> str | None:
    try:
        xml = base64.b64decode(saml_response_b64).decode("utf-8", "replace")
    except Exception:
        return None
    m = re.search(r'<(?:\w+:)?Response\b[^>]*\bInResponseTo="([^"]+)"', xml)
    return m.group(1) if m else None


@public.post("/sso/{provider_id}/acs")
async def saml_acs(provider_id: str, request: Request, db: Session = DB):
    p, cfg = _provider_or_404(db, provider_id)
    if p.kind != "saml":
        raise HTTPException(404, "not a SAML provider")
    form = dict(await request.form())
    if "SAMLResponse" not in form:
        raise HTTPException(400, "missing SAMLResponse")
    pending = _saml_consume(_in_response_to(form["SAMLResponse"]))
    if pending is None:
        raise HTTPException(401, "unsolicited, replayed or expired SAML response; start the login again")
    try:
        who = saml.finish_login(p.id, cfg, form, request_id=_in_response_to(form["SAMLResponse"]))
    except saml.SAMLError as e:
        raise HTTPException(401, str(e))
    return _login_response(db, cfg, who, core.safe_next(pending.get("next")))


@public.get("/sso/{provider_id}/metadata")
def saml_metadata(provider_id: str, db: Session = DB):
    p, cfg = _provider_or_404(db, provider_id)
    if p.kind != "saml":
        raise HTTPException(404, "not a SAML provider")
    try:
        return Response(saml.metadata_xml(p.id, cfg), media_type="application/samlmetadata+xml")
    except saml.SAMLError as e:
        raise HTTPException(500, str(e))


@public.get("/sso/me")
def me(ident: Identity = Depends(current_identity)):
    return {**ident.principal().to_dict(), "account": ident.to_dict()}


@public.api_route("/sso/logout", methods=["GET", "POST"])
def logout():
    resp = JSONResponse({"signed_out": True})
    resp.delete_cookie(core.SESSION_COOKIE, path="/")
    return resp

