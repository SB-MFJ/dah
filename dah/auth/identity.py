"""Who is calling: admin token, local or provisioned user (session / API token), or an IdP-only principal."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import ApiToken, User, dumps, utcnow
from ..sso import core
from .passwords import DUMMY_HASH, hash_password, verify_password

PAT_PREFIX = "dah_pat_"
CSRF_HEADER = "x-dah-csrf"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
USERNAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,254}$")


class AuthError(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


@dataclass
class Identity:
    kind: str  # "admin-token" | "user" | "principal" (IdP identity without an account)
    username: str
    is_admin: bool
    groups: list[str] = field(default_factory=list)
    email: str | None = None
    name: str | None = None
    user_id: str | None = None
    provider: str = "local"
    subject: str = ""
    attributes: dict = field(default_factory=dict)
    via: str = "bearer"  # bearer | cookie | pat | admin-token

    def principal(self) -> core.Principal:
        return core.Principal(subject=self.subject or self.username, provider=self.provider, email=self.email,
                              name=self.name, groups=list(self.groups), attributes=dict(self.attributes))

    def to_dict(self) -> dict:
        return {"kind": self.kind, "username": self.username, "is_admin": self.is_admin, "groups": self.groups,
                "email": self.email, "name": self.name, "user_id": self.user_id, "provider": self.provider, "via": self.via}


def identity_for_user(u: User, via: str) -> Identity:
    return Identity(kind="user", username=u.username, is_admin=u.admin, groups=u.groups, email=u.email,
                    name=u.display_name, user_id=u.id, provider=u.source, subject=u.external_id or u.username,
                    attributes=u.attributes, via=via)


# --------------------------------------------------------------------------- sessions


def issue_user_session(u: User) -> str:
    return core.issue_session(identity_for_user(u, "cookie").principal(), uid=u.id, sv=u.session_version)


def _from_session_claims(db: Session, claims: dict, via: str) -> Identity | None:
    uid = claims.get("uid")
    if uid is None:  # IdP-only principal session (no local account)
        return Identity(kind="principal", username=claims.get("email") or claims["sub"], is_admin=False,
                        groups=list(claims.get("groups") or []), email=claims.get("email"), name=claims.get("name"),
                        provider=claims.get("idp", ""), subject=claims["sub"], attributes=dict(claims.get("attrs") or {}), via=via)
    u = db.get(User, uid)
    if u is None or u.disabled or claims.get("sv") != u.session_version:
        return None
    return identity_for_user(u, via)


# --------------------------------------------------------------------------- API tokens


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_api_token(db: Session, user: User, name: str, expires_at: datetime | None = None) -> tuple[ApiToken, str]:
    raw = PAT_PREFIX + secrets.token_urlsafe(32)
    t = ApiToken(user_id=user.id, name=name[:255] or "token", token_hash=_token_hash(raw), prefix=raw[:12], expires_at=expires_at)
    db.add(t)
    db.flush()
    return t, raw


def _identity_from_pat(db: Session, token: str) -> Identity | None:
    t = db.scalar(select(ApiToken).where(ApiToken.token_hash == _token_hash(token)))
    if t is None:
        return None
    exp = t.expires_at
    if exp is not None and (exp if exp.tzinfo else exp.replace(tzinfo=timezone.utc)) < datetime.now(timezone.utc):
        return None
    u = db.get(User, t.user_id)
    if u is None or u.disabled:
        return None
    t.last_used_at = utcnow()
    return identity_for_user(u, "pat")


# --------------------------------------------------------------------------- request authentication


def authenticate(request: Request, db: Session) -> Identity | None:
    """Resolve the caller. Raises AuthError for presented-but-invalid credentials."""
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("x-api-key")
    if token:
        if hmac.compare_digest(token.encode(), get_settings().admin_token.encode()):
            return Identity(kind="admin-token", username="admin", is_admin=True, provider="admin-token", via="admin-token")
        if token.startswith(PAT_PREFIX):
            ident = _identity_from_pat(db, token)
            if ident is None:
                raise AuthError("invalid, expired or revoked API token")
            return ident
        claims = core.decode_session(token)
        if claims is not None:
            ident = _from_session_claims(db, claims, "bearer")
            if ident is None:
                raise AuthError("session is no longer valid")
            return ident
        ident = _identity_from_idp_bearer(db, token)
        if ident is not None:
            return ident
        raise AuthError("invalid or expired token")

    cookie = request.cookies.get(core.SESSION_COOKIE)
    if cookie:
        claims = core.decode_session(cookie)
        ident = _from_session_claims(db, claims, "cookie") if claims else None
        if ident is None:
            return None
        if request.method not in SAFE_METHODS and request.headers.get(CSRF_HEADER) != "1":
            raise AuthError(f"missing {CSRF_HEADER} header for cookie-authenticated request", 403)
        return ident
    return None


def _identity_from_idp_bearer(db: Session, token: str) -> Identity | None:
    """OIDC provider-issued JWT (machine clients). Maps to the provisioned user when one exists."""
    from ..db import IdentityProvider
    from ..sso import oidc

    iss = core.unverified_issuer(token)
    if not iss:
        return None
    for prov in db.scalars(select(IdentityProvider).where(IdentityProvider.kind == "oidc", IdentityProvider.enabled.is_(True))):
        cfg = core.load_config(prov)
        if cfg.get("allow_bearer") and cfg.get("issuer", "").rstrip("/") == iss.rstrip("/"):
            try:
                p = oidc.verify_bearer(prov.id, cfg, token)
            except oidc.OIDCError as e:
                raise AuthError(str(e))
            u = db.scalar(select(User).where(User.source == prov.id, User.external_id == p.subject))
            if u is not None:
                if u.disabled:
                    raise AuthError("account disabled", 403)
                ident = identity_for_user(u, "bearer")
                ident.groups = sorted(set(u.local_groups) | set(p.groups))  # token groups are current
                return ident
            return Identity(kind="principal", username=p.email or p.subject, is_admin=False, groups=p.groups, email=p.email,
                            name=p.name, provider=prov.id, subject=p.subject, attributes=p.attributes)
    return None


# --------------------------------------------------------------------------- local login + throttling

_failures: dict[str, deque] = defaultdict(deque)
_lock = threading.Lock()
WINDOW_S, MAX_FAILURES = 15 * 60, 10


def _throttle_key(username: str, ip: str) -> str:
    return f"{username.lower()}|{ip}"


def check_throttle(username: str, ip: str) -> None:
    now = time.time()
    with _lock:
        q = _failures[_throttle_key(username, ip)]
        while q and q[0] < now - WINDOW_S:
            q.popleft()
        if len(q) >= MAX_FAILURES:
            raise AuthError("too many failed attempts; try again later", 429)


def record_failure(username: str, ip: str) -> None:
    with _lock:
        _failures[_throttle_key(username, ip)].append(time.time())


def clear_failures(username: str, ip: str) -> None:
    with _lock:
        _failures.pop(_throttle_key(username, ip), None)


def find_user(db: Session, username: str) -> User | None:
    return db.scalar(select(User).where(func.lower(User.username) == username.lower()))


def local_login(db: Session, username: str, password: str) -> User:
    u = find_user(db, username)
    if u is None or u.source != "local" or not u.password_hash:
        verify_password(password, DUMMY_HASH)
        raise AuthError("invalid username or password")
    if not verify_password(password, u.password_hash):
        raise AuthError("invalid username or password")
    if u.disabled:
        raise AuthError("account disabled", 403)
    u.last_login_at = utcnow()
    return u


def check_password_policy(password: str) -> None:
    n = get_settings().password_min_length
    if len(password) < n:
        raise AuthError(f"password must be at least {n} characters", 400)


def set_password(u: User, password: str) -> None:
    check_password_policy(password)
    u.password_hash = hash_password(password)
    u.session_version += 1  # sign out everywhere


def create_local_user(db: Session, username: str, password: str | None = None, *, email: str | None = None,
                      display_name: str | None = None, groups: list[str] | None = None, is_admin: bool = False) -> User:
    if not USERNAME.match(username):
        raise AuthError("username may contain letters, digits and . _ @ + -", 400)
    if find_user(db, username):
        raise AuthError("username already exists", 409)
    u = User(username=username, email=email, display_name=display_name, source="local",
             groups_json=dumps(sorted(set(groups or []))), is_admin=is_admin)
    if password:
        check_password_policy(password)
        u.password_hash = hash_password(password)
    db.add(u)
    db.flush()
    return u


# --------------------------------------------------------------------------- external (JIT) provisioning


def provision_external(db: Session, p: core.Principal, admin_groups: list[str] | None = None) -> User:
    """Create or refresh the account for an OIDC / SAML / LDAP login. Raises if disabled."""
    u = db.scalar(select(User).where(User.source == p.provider, User.external_id == p.subject))
    if u is None:
        base = (p.email or p.subject or "user").strip()
        candidate = base if USERNAME.match(base) and not find_user(db, base) else None
        if candidate is None:
            stem = re.sub(r"[^A-Za-z0-9._@+-]", "_", f"{p.provider}.{base}")[:240].lstrip("._-+@") or "user"
            candidate, i = stem, 1
            while find_user(db, candidate):
                i += 1
                candidate = f"{stem}.{i}"
        u = User(username=candidate, source=p.provider, external_id=p.subject)
        db.add(u)
    if u.disabled:
        raise AuthError("account disabled", 403)
    u.email = p.email or u.email
    u.display_name = p.name or u.display_name
    u.idp_groups_json = dumps(sorted(set(p.groups)))
    u.attributes_json = dumps(p.attributes or {})
    u.idp_admin = bool(set(admin_groups or []) & set(p.groups))
    u.last_login_at = utcnow()
    db.flush()
    return u
