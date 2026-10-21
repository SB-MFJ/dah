"""Site login (local + password providers), session management, personal API tokens, user administration."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth import identity as idm
from ..config import get_settings
from ..db import ApiToken, IdentityProvider, User, dumps
from ..sso import core
from .deps import DB, Admin, Caller, require_account

auth = APIRouter(prefix="/auth", tags=["auth"])
users = APIRouter(prefix="/api", tags=["users"], dependencies=[Admin])

Account = Depends(require_account)


def set_session_cookie(resp: Response, token: str) -> Response:
    s = get_settings()
    resp.set_cookie(core.SESSION_COOKIE, token, max_age=s.session_ttl_s, httponly=True,
                    secure=s.public_base_url.startswith("https://"), samesite="lax", path="/")
    return resp


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "?"


def _user(db: Session, ident: idm.Identity) -> User:
    u = db.get(User, ident.user_id)
    if u is None:
        raise HTTPException(404, "user not found")
    return u


# =========================================================================== login


class LoginIn(BaseModel):
    username: str
    password: str
    provider: str = "local"  # "local" or an LDAP provider id
    next: str | None = None


@auth.get("/methods")
def methods(db: Session = DB):
    """Login options for the sign-in screen (public)."""
    password, redirect = [], []
    if get_settings().local_login_enabled:
        password.append({"id": "local", "display_name": "Local account"})
    for p in db.scalars(select(IdentityProvider).where(IdentityProvider.enabled.is_(True)).order_by(IdentityProvider.display_name)):
        if p.kind == "ldap":
            password.append({"id": p.id, "display_name": p.display_name})
        else:
            redirect.append({"id": p.id, "kind": p.kind, "display_name": p.display_name, "login_url": f"/sso/{p.id}/login"})
    return {"password": password, "redirect": redirect}


def password_login(db: Session, provider: str, username: str, password: str, ip: str) -> User:
    if not username or not password:
        raise idm.AuthError("username and password are required", 400)
    idm.check_throttle(username, ip)
    try:
        if provider == "local":
            if not get_settings().local_login_enabled:
                raise idm.AuthError("local login is disabled", 403)
            u = idm.local_login(db, username, password)
        else:
            p = core.get_provider(db, provider)
            if p is None or p.kind != "ldap":
                raise idm.AuthError("unknown login provider", 400)
            from ..auth import ldap

            cfg = core.load_config(p)
            try:
                principal = ldap.authenticate(p.id, cfg, username, password)
            except ldap.LDAPAuthError as e:
                raise idm.AuthError(str(e), e.status)
            u = idm.provision_external(db, principal, cfg.get("admin_groups"))
    except idm.AuthError as e:
        if e.status == 401:
            idm.record_failure(username, ip)
        raise
    idm.clear_failures(username, ip)
    return u


@auth.post("/login")
async def login(request: Request, db: Session = DB):
    """JSON (``{username, password, provider}``) or an HTML form post (redirects to ``next``)."""
    is_form = request.headers.get("content-type", "").startswith(("application/x-www-form-urlencoded", "multipart/form-data"))
    if is_form:
        f = await request.form()
        body = LoginIn(username=str(f.get("username", "")), password=str(f.get("password", "")),
                       provider=str(f.get("provider") or "local"), next=str(f.get("next") or "") or None)
    else:
        try:
            body = LoginIn(**(await request.json()))
        except Exception:
            raise HTTPException(400, "expected JSON {username, password, provider}")
    try:
        u = password_login(db, body.provider, body.username.strip(), body.password, _client_ip(request))
    except idm.AuthError as e:
        if is_form:
            return RedirectResponse(f"/sso?error={'locked' if e.status == 429 else 'invalid'}", status_code=303)
        raise HTTPException(e.status, str(e))
    token = idm.issue_user_session(u)
    if is_form:
        return set_session_cookie(RedirectResponse(core.safe_next(body.next) or "/", status_code=303), token)
    return set_session_cookie(JSONResponse({"user": idm.identity_for_user(u, "cookie").to_dict()}), token)


@auth.post("/logout")
def logout():
    resp = JSONResponse({"signed_out": True})
    resp.delete_cookie(core.SESSION_COOKIE, path="/")
    return resp


@auth.get("/me")
def me(ident: idm.Identity = Caller):
    return ident.to_dict()


class PasswordIn(BaseModel):
    current_password: str
    new_password: str


@auth.post("/password")
def change_password(body: PasswordIn, ident: idm.Identity = Account, db: Session = DB):
    """Change your own local password. Signs out all other sessions; returns a fresh cookie."""
    u = _user(db, ident)
    if u.source != "local":
        raise HTTPException(400, f"password is managed by identity provider {u.source!r}")
    if not idm.verify_password(body.current_password, u.password_hash):
        raise HTTPException(403, "current password is incorrect")
    try:
        idm.set_password(u, body.new_password)
    except idm.AuthError as e:
        raise HTTPException(e.status, str(e))
    db.flush()
    return set_session_cookie(JSONResponse({"changed": True}), idm.issue_user_session(u))


# =========================================================================== personal API tokens


class TokenIn(BaseModel):
    name: str
    expires_in_days: int | None = Field(None, ge=1, le=3650)


def _token_dict(t: ApiToken) -> dict:
    return {"id": t.id, "name": t.name, "prefix": t.prefix, "expires_at": t.expires_at, "last_used_at": t.last_used_at, "created_at": t.created_at}


@auth.get("/tokens")
def list_tokens(ident: idm.Identity = Account, db: Session = DB):
    rows = db.scalars(select(ApiToken).where(ApiToken.user_id == ident.user_id).order_by(ApiToken.created_at.desc()))
    return {"items": [_token_dict(t) for t in rows]}


@auth.post("/tokens", status_code=201)
def create_token(body: TokenIn, ident: idm.Identity = Account, db: Session = DB):
    """Create a personal API token (shown once). It acts with your permissions, e.g. for MCP clients or scripts."""
    exp = datetime.now(timezone.utc) + timedelta(days=body.expires_in_days) if body.expires_in_days else None
    t, raw = idm.create_api_token(db, _user(db, ident), body.name, exp)
    return {**_token_dict(t), "token": raw}


@auth.delete("/tokens/{token_id}")
def revoke_token(token_id: str, ident: idm.Identity = Account, db: Session = DB):
    t = db.get(ApiToken, token_id)
    if t is None or t.user_id != ident.user_id:
        raise HTTPException(404, "token not found")
    db.delete(t)
    return {"revoked": token_id}


# =========================================================================== user administration


class UserIn(BaseModel):
    username: str
    password: str | None = None
    email: str | None = None
    display_name: str | None = None
    groups: list[str] = Field(default_factory=list)
    is_admin: bool = False


class UserUpdate(BaseModel):
    email: str | None = None
    display_name: str | None = None
    groups: list[str] | None = None
    is_admin: bool | None = None
    disabled: bool | None = None


class SetPasswordIn(BaseModel):
    password: str


def user_dict(u: User) -> dict:
    return {"id": u.id, "username": u.username, "email": u.email, "display_name": u.display_name, "source": u.source,
            "local_groups": u.local_groups, "idp_groups": u.idp_groups, "groups": u.groups, "is_admin": u.is_admin,
            "idp_admin": u.idp_admin, "admin": u.admin, "disabled": u.disabled, "has_password": bool(u.password_hash),
            "created_at": u.created_at, "last_login_at": u.last_login_at}


def _get_user(db: Session, user_id: str) -> User:
    u = db.get(User, user_id) or idm.find_user(db, user_id)
    if u is None:
        raise HTTPException(404, "user not found")
    return u


@users.get("/users")
def list_users(db: Session = DB):
    return {"items": [user_dict(u) for u in db.scalars(select(User).order_by(User.username))]}


@users.post("/users", status_code=201)
def create_user(body: UserIn, db: Session = DB):
    try:
        u = idm.create_local_user(db, body.username.strip(), body.password, email=body.email, display_name=body.display_name,
                                  groups=body.groups, is_admin=body.is_admin)
    except idm.AuthError as e:
        raise HTTPException(e.status, str(e))
    return user_dict(u)


@users.get("/users/{user_id}")
def get_user(user_id: str, db: Session = DB):
    return user_dict(_get_user(db, user_id))


@users.put("/users/{user_id}")
def update_user(user_id: str, body: UserUpdate, ident: idm.Identity = Admin, db: Session = DB):
    u = _get_user(db, user_id)
    ch = body.model_dump(exclude_unset=True)
    if u.id == ident.user_id and (ch.get("disabled") or ch.get("is_admin") is False):
        raise HTTPException(400, "you cannot disable or demote yourself")
    if "email" in ch:
        u.email = ch["email"]
    if "display_name" in ch:
        u.display_name = ch["display_name"]
    if "groups" in ch and ch["groups"] is not None:
        u.groups_json = dumps(sorted({g.strip() for g in ch["groups"] if g.strip()}))
    if "is_admin" in ch and ch["is_admin"] is not None:
        u.is_admin = ch["is_admin"]
    if "disabled" in ch and ch["disabled"] is not None:
        if ch["disabled"] and not u.disabled:
            u.session_version += 1
        u.disabled = ch["disabled"]
    db.flush()
    return user_dict(u)


@users.post("/users/{user_id}/password")
def admin_set_password(user_id: str, body: SetPasswordIn, db: Session = DB):
    u = _get_user(db, user_id)
    if u.source != "local":
        raise HTTPException(400, "only local accounts have passwords")
    try:
        idm.set_password(u, body.password)
    except idm.AuthError as e:
        raise HTTPException(e.status, str(e))
    return {"changed": True}


@users.post("/users/{user_id}/revoke-sessions")
def revoke_sessions(user_id: str, db: Session = DB):
    u = _get_user(db, user_id)
    u.session_version += 1
    return {"revoked": True}


@users.delete("/users/{user_id}")
def delete_user(user_id: str, ident: idm.Identity = Admin, db: Session = DB):
    u = _get_user(db, user_id)
    if u.id == ident.user_id:
        raise HTTPException(400, "you cannot delete yourself")
    db.delete(u)
    return {"deleted": u.username}


@users.get("/groups")
def list_groups(db: Session = DB):
    """Every group name known from local assignments and IdP logins (for grant autocomplete)."""
    names: set[str] = set()
    for u in db.scalars(select(User)):
        names |= set(u.groups)
    return {"items": sorted(names)}
