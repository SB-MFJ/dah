"""LDAP / Active Directory login: service-account search, then bind as the user to verify the password.

Groups come from the user's ``memberOf`` attribute (CN of each DN) and/or a group search
(``group_filter`` with ``{user_dn}`` / ``{username}`` placeholders). All user input placed into
filters is escaped (RFC 4515).
"""

from __future__ import annotations

import ssl
from typing import Callable

from ldap3 import ALL, BASE, SUBTREE, Connection, Server, Tls
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars
from ldap3.utils.dn import parse_dn

from ..sso.core import Principal


class LDAPAuthError(Exception):
    def __init__(self, message: str, status: int = 401):
        super().__init__(message)
        self.status = status


def _server(cfg: dict) -> Server:
    url = cfg["server_url"]
    tls = None
    if url.startswith("ldaps://") or cfg.get("start_tls"):
        validate = ssl.CERT_REQUIRED if cfg.get("tls_verify", True) else ssl.CERT_NONE
        tls = Tls(validate=validate, ca_certs_file=cfg.get("ca_cert_file") or None)
    return Server(url, use_ssl=url.startswith("ldaps://"), tls=tls, get_info=ALL, connect_timeout=int(cfg.get("timeout_s", 10)))


def _connect(cfg: dict, user: str | None, password: str | None) -> Connection:
    """Bound connection. Swappable in tests (ldap3 MOCK_SYNC)."""
    conn = Connection(_server(cfg), user=user, password=password, receive_timeout=int(cfg.get("timeout_s", 10)), raise_exceptions=False)
    conn.open()
    if cfg.get("start_tls") and not cfg["server_url"].startswith("ldaps://"):
        conn.start_tls()
    conn.bind()
    return conn


connect: Callable[[dict, str | None, str | None], Connection] = _connect


def _first(entry, attr: str):
    if not attr or attr not in entry:
        return None
    v = entry[attr].value
    return v[0] if isinstance(v, list) and v else v


def _cn(dn: str) -> str:
    try:
        for typ, val, _ in parse_dn(dn):
            if typ.lower() == "cn":
                return val
    except Exception:
        pass
    return dn


def authenticate(provider_id: str, cfg: dict, username: str, password: str) -> Principal:
    if not password:  # an empty password would be an anonymous ("unauthenticated") bind that succeeds
        raise LDAPAuthError("invalid username or password")
    try:
        svc = connect(cfg, cfg.get("bind_dn") or None, cfg.get("bind_password") or None)
        if not svc.bound:
            raise LDAPAuthError("LDAP service bind failed; check bind_dn / bind_password", 502)
        uattr = cfg.get("username_attribute") or "uid"
        flt = (cfg.get("user_filter") or f"({uattr}={{username}})").replace("{username}", escape_filter_chars(username))
        attrs = [a for a in {cfg.get("email_attribute"), cfg.get("name_attribute"), cfg.get("groups_attribute"), uattr,
                             *(cfg.get("attribute_names") or [])} if a]
        svc.search(cfg["user_search_base"], flt, search_scope=SUBTREE, attributes=attrs, size_limit=2)
        entries = list(svc.entries)
        if len(entries) != 1:
            raise LDAPAuthError("invalid username or password")
        entry = entries[0]
        user_dn = entry.entry_dn

        user_conn = connect(cfg, user_dn, password)
        ok = user_conn.bound
        user_conn.unbind()
        if not ok:
            raise LDAPAuthError("invalid username or password")

        groups: set[str] = set()
        gattr = cfg.get("groups_attribute")
        if gattr and gattr in entry:
            vals = entry[gattr].value
            for dn in vals if isinstance(vals, list) else [vals]:
                if dn:
                    groups.add(_cn(str(dn)))
        if cfg.get("group_search_base"):
            gflt = (cfg.get("group_filter") or "(|(member={user_dn})(uniqueMember={user_dn}))")
            gflt = gflt.replace("{user_dn}", escape_filter_chars(user_dn)).replace("{username}", escape_filter_chars(username))
            name_attr = cfg.get("group_name_attribute") or "cn"
            svc.search(cfg["group_search_base"], gflt, search_scope=SUBTREE, attributes=[name_attr])
            for g in svc.entries:
                v = _first(g, name_attr)
                groups.add(str(v) if v else _cn(g.entry_dn))
        svc.unbind()
    except LDAPException as e:
        raise LDAPAuthError(f"LDAP error: {e}", 502) from e

    extra = {}
    for a in cfg.get("attribute_names") or []:
        if a in entry:
            v = entry[a].value
            extra[a] = [str(x) for x in v] if isinstance(v, list) else (str(v) if v is not None else None)
    email = _first(entry, cfg.get("email_attribute") or "mail")
    return Principal(subject=user_dn, provider=provider_id, email=str(email) if email else None,
                     name=str(_first(entry, cfg.get("name_attribute") or "cn") or username), groups=sorted(groups), attributes=extra)


def test_connection(cfg: dict) -> dict:
    """Service bind + base lookup, for the admin UI."""
    try:
        c = connect(cfg, cfg.get("bind_dn") or None, cfg.get("bind_password") or None)
        if not c.bound:
            return {"ok": False, "error": f"bind failed: {c.result.get('description')}"}
        c.search(cfg["user_search_base"], "(objectClass=*)", search_scope=BASE)
        found = bool(c.entries)
        c.unbind()
        return {"ok": found, "error": None if found else "user_search_base not found"}
    except LDAPException as e:
        return {"ok": False, "error": str(e)}
