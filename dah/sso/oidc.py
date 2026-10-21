"""OpenID Connect: authorization-code flow with PKCE, ID-token validation via JWKS, bearer tokens."""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from urllib.parse import urlencode

import httpx
import jwt

from ..config import get_settings
from .core import Principal, as_list

# Module-level client so tests can swap in a mock transport.
http = httpx.Client(timeout=15, follow_redirects=True)

_DISCOVERY_TTL = 3600
_discovery: dict[str, tuple[float, dict]] = {}
_jwks: dict[str, tuple[float, dict]] = {}


class OIDCError(Exception):
    pass


def discovery(cfg: dict) -> dict:
    url = cfg.get("discovery_url") or cfg["issuer"].rstrip("/") + "/.well-known/openid-configuration"
    hit = _discovery.get(url)
    if hit and hit[0] > time.time():
        return hit[1]
    r = http.get(url)
    if r.status_code != 200:
        raise OIDCError(f"discovery failed ({r.status_code}) at {url}")
    doc = r.json()
    _discovery[url] = (time.time() + _DISCOVERY_TTL, doc)
    return doc


def _jwk_for(jwks_uri: str, kid: str | None) -> jwt.PyJWK:
    for attempt in range(2):
        hit = _jwks.get(jwks_uri)
        if not hit or hit[0] < time.time() or attempt == 1:  # refetch on unknown kid (key rotation)
            r = http.get(jwks_uri)
            if r.status_code != 200:
                raise OIDCError(f"JWKS fetch failed ({r.status_code})")
            _jwks[jwks_uri] = (time.time() + _DISCOVERY_TTL, r.json())
            hit = _jwks[jwks_uri]
        keys = hit[1].get("keys", [])
        for k in keys:
            if kid is None or k.get("kid") == kid:
                return jwt.PyJWK(k)
    raise OIDCError("signing key not found in JWKS")


def verify_jwt(cfg: dict, token: str, audience: str | None, nonce: str | None = None) -> dict:
    disc = discovery(cfg)
    header = jwt.get_unverified_header(token)
    alg = header.get("alg", "RS256")
    if alg.upper().startswith("HS") or alg == "none":
        raise OIDCError(f"unsupported token algorithm {alg}")
    key = _jwk_for(disc["jwks_uri"], header.get("kid"))
    try:
        claims = jwt.decode(
            token, key.key, algorithms=[alg], audience=audience, issuer=disc.get("issuer") or cfg["issuer"],
            options={"require": ["exp", "iss", "sub"], "verify_aud": audience is not None}, leeway=60,
        )
    except jwt.PyJWTError as e:
        raise OIDCError(f"invalid token: {e}") from e
    if nonce is not None and claims.get("nonce") != nonce:
        raise OIDCError("nonce mismatch")
    return claims


def principal_from_claims(provider_id: str, cfg: dict, claims: dict) -> Principal:
    attrs = {k: claims[k] for k in cfg.get("attribute_claims") or [] if k in claims}
    return Principal(
        subject=str(claims["sub"]),
        provider=provider_id,
        email=claims.get(cfg.get("email_claim") or "email"),
        name=claims.get(cfg.get("name_claim") or "name"),
        groups=as_list(claims.get(cfg.get("groups_claim") or "groups")),
        attributes=attrs,
    )


def redirect_uri(provider_id: str) -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/sso/{provider_id}/callback"


def begin_login(provider_id: str, cfg: dict) -> tuple[str, dict]:
    """Returns (authorization URL, flow state to keep in a signed cookie)."""
    disc = discovery(cfg)
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {
        "response_type": "code",
        "client_id": cfg["client_id"],
        "redirect_uri": redirect_uri(provider_id),
        "scope": " ".join(cfg.get("scopes") or ["openid"]),
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return disc["authorization_endpoint"] + "?" + urlencode(params), {"state": state, "nonce": nonce, "verifier": verifier}


def finish_login(provider_id: str, cfg: dict, code: str, flow: dict) -> Principal:
    disc = discovery(cfg)
    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri(provider_id),
        "code_verifier": flow["verifier"],
        "client_id": cfg["client_id"],
    }
    auth = None
    if cfg.get("client_secret"):
        methods = disc.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
        if "client_secret_basic" in methods:
            auth = (cfg["client_id"], cfg["client_secret"])
        else:
            data["client_secret"] = cfg["client_secret"]
    r = http.post(disc["token_endpoint"], data=data, auth=auth, headers={"Accept": "application/json"})
    if r.status_code != 200:
        raise OIDCError(f"token exchange failed ({r.status_code}): {r.text[:200]}")
    tokens = r.json()
    if "id_token" not in tokens:
        raise OIDCError("token response has no id_token (is the 'openid' scope set?)")
    claims = verify_jwt(cfg, tokens["id_token"], audience=cfg["client_id"], nonce=flow["nonce"])
    # Some IdPs only put groups in userinfo.
    groups_claim = cfg.get("groups_claim") or "groups"
    if groups_claim not in claims and disc.get("userinfo_endpoint") and tokens.get("access_token"):
        u = http.get(disc["userinfo_endpoint"], headers={"Authorization": f"Bearer {tokens['access_token']}"})
        if u.status_code == 200:
            info = u.json()
            if info.get("sub") == claims["sub"]:
                claims = {**info, **claims}
    return principal_from_claims(provider_id, cfg, claims)


def verify_bearer(provider_id: str, cfg: dict, token: str) -> Principal:
    claims = verify_jwt(cfg, token, audience=cfg.get("audience") or cfg["client_id"])
    return principal_from_claims(provider_id, cfg, claims)
