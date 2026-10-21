"""Identity provider registry, principals and dah session tokens."""

from __future__ import annotations

import base64
import json
import time
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from typing import Any

import jwt
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from sqlalchemy.orm import Session

from .. import settings_store
from ..config import get_settings
from ..db import IdentityProvider

SESSION_COOKIE = "dah_session"
SESSION_TYP = "dah-session"
FLOW_TYP = "dah-sso-flow"

# Config keys holding secrets: encrypted at rest and masked in API responses.
SECRET_KEYS = {"client_secret", "sp_private_key"}

OIDC_DEFAULTS = {
    "issuer": "",
    "discovery_url": "",
    "client_id": "",
    "client_secret": "",
    "scopes": ["openid", "email", "profile"],
    "email_claim": "email",
    "name_claim": "name",
    "groups_claim": "groups",
    "attribute_claims": [],  # extra claims copied into principal.attributes
    "allow_bearer": True,  # accept IdP-issued JWT access/ID tokens directly on /data
    "audience": "",  # expected aud for bearer tokens (default client_id)
}
SAML_DEFAULTS = {
    "idp_entity_id": "",
    "idp_sso_url": "",
    "idp_slo_url": "",
    "idp_x509_cert": "",
    "sp_entity_id": "",  # default: <base>/sso/<id>/metadata
    "sp_x509_cert": "",
    "sp_private_key": "",
    "name_id_format": "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
    "email_attribute": "email",
    "name_attribute": "displayName",
    "groups_attribute": "groups",
    "attribute_names": [],  # extra attributes copied into principal.attributes
    "want_assertions_signed": True,
    "want_messages_signed": False,
}


@dataclass
class Principal:
    subject: str
    provider: str
    email: str | None = None
    name: str | None = None
    groups: list[str] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    def claim(self, path: str) -> Any:
        """Resolve 'email' / 'subject' / 'groups' / 'provider' / 'name' / 'attributes.<key>'."""
        if path.startswith("attributes."):
            return self.attributes.get(path.split(".", 1)[1])
        return getattr(self, path, None)


# --------------------------------------------------------------------------- provider config


def defaults_for(kind: str) -> dict:
    return dict(OIDC_DEFAULTS if kind == "oidc" else SAML_DEFAULTS)


def store_config(kind: str, cfg: dict, previous: dict | None = None) -> str:
    merged = {**defaults_for(kind), **{k: v for k, v in cfg.items() if k in defaults_for(kind)}}
    for k in SECRET_KEYS & set(merged):
        v = merged[k]
        if v in (None, "") and previous and previous.get(k):
            merged[k] = previous[k]  # keep existing encrypted secret
        elif v:
            merged[k] = "enc:" + settings_store.encrypt(v)
    return json.dumps(merged)


def load_config(p: IdentityProvider) -> dict:
    """Decrypted config for runtime use."""
    cfg = {**defaults_for(p.kind), **json.loads(p.config_json or "{}")}
    for k in SECRET_KEYS & set(cfg):
        if isinstance(cfg[k], str) and cfg[k].startswith("enc:"):
            cfg[k] = settings_store.decrypt(cfg[k][4:])
    return cfg


def public_config(p: IdentityProvider) -> dict:
    raw = {**defaults_for(p.kind), **json.loads(p.config_json or "{}")}
    for k in SECRET_KEYS & set(raw):
        raw[k] = {"set": bool(raw[k])}
    return raw


def provider_dict(p: IdentityProvider) -> dict:
    base = get_settings().public_base_url.rstrip("/")
    d = {"id": p.id, "kind": p.kind, "display_name": p.display_name, "enabled": p.enabled, "config": public_config(p),
         "login_url": f"{base}/sso/{p.id}/login"}
    if p.kind == "oidc":
        d["redirect_uri"] = f"{base}/sso/{p.id}/callback"
    else:
        d["acs_url"] = f"{base}/sso/{p.id}/acs"
        d["metadata_url"] = f"{base}/sso/{p.id}/metadata"
    return d


def get_provider(db: Session, provider_id: str) -> IdentityProvider | None:
    p = db.get(IdentityProvider, provider_id)
    return p if p is not None and p.enabled else None


# --------------------------------------------------------------------------- tokens


@lru_cache
def _signing_key(secret: str, purpose: str) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=b"dah-sso-v1", info=purpose.encode()).derive(secret.encode())


def _key(purpose: str) -> bytes:
    return _signing_key(get_settings().secret_key, purpose)


def issue_session(p: Principal, ttl: int | None = None) -> str:
    now = int(time.time())
    ttl = ttl or get_settings().session_ttl_s
    claims = {"typ": SESSION_TYP, "iat": now, "exp": now + ttl, "sub": p.subject, "idp": p.provider,
              "email": p.email, "name": p.name, "groups": p.groups, "attrs": p.attributes}
    return jwt.encode(claims, _key("session"), algorithm="HS256")


def verify_session(token: str) -> Principal | None:
    try:
        c = jwt.decode(token, _key("session"), algorithms=["HS256"], options={"require": ["exp", "sub", "typ"]})
    except jwt.PyJWTError:
        return None
    if c.get("typ") != SESSION_TYP:
        return None
    return Principal(subject=c["sub"], provider=c.get("idp", ""), email=c.get("email"), name=c.get("name"),
                     groups=list(c.get("groups") or []), attributes=dict(c.get("attrs") or {}))


def sign_flow(data: dict, ttl: int = 600) -> str:
    now = int(time.time())
    return jwt.encode({**data, "typ": FLOW_TYP, "iat": now, "exp": now + ttl}, _key("flow"), algorithm="HS256")


def verify_flow(token: str | None) -> dict | None:
    if not token:
        return None
    try:
        c = jwt.decode(token, _key("flow"), algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    return c if c.get("typ") == FLOW_TYP else None


def unverified_issuer(token: str) -> str | None:
    """Peek at the iss claim to route a bearer token to the right provider (verified afterwards)."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("iss")
    except Exception:
        return None


def safe_next(url: str | None) -> str | None:
    """Only allow local redirects after login (no open redirect)."""
    if url and url.startswith("/") and not url.startswith("//") and "\\" not in url:
        return url
    return None


def as_list(v: Any) -> list[str]:
    if v is None:
        return []
    if isinstance(v, (list, tuple, set)):
        return [str(x) for x in v]
    if isinstance(v, str) and "," in v:
        return [x.strip() for x in v.split(",") if x.strip()]
    return [str(v)]
