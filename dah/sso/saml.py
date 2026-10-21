"""SAML 2.0 service provider (python3-saml): SP metadata, AuthnRequest redirect, signed-assertion ACS."""

from __future__ import annotations

from urllib.parse import urlparse

from ..config import get_settings
from .core import Principal, as_list


class SAMLError(Exception):
    pass


def _lib():
    try:
        from onelogin.saml2.auth import OneLogin_Saml2_Auth
        from onelogin.saml2.settings import OneLogin_Saml2_Settings
    except ImportError as e:  # pragma: no cover
        raise SAMLError("SAML support needs the 'saml' extra: pip install 'dah[saml]' (included in the Docker image)") from e
    return OneLogin_Saml2_Auth, OneLogin_Saml2_Settings


def _wrap(fn):
    """Surface python3-saml configuration/validation errors as SAMLError (HTTP 4xx/5xx, not a crash)."""

    def inner(*a, **kw):
        try:
            return fn(*a, **kw)
        except SAMLError:
            raise
        except Exception as e:
            from onelogin.saml2.errors import OneLogin_Saml2_Error, OneLogin_Saml2_ValidationError

            if isinstance(e, (OneLogin_Saml2_Error, OneLogin_Saml2_ValidationError)):
                raise SAMLError(f"SAML error: {e}") from e
            raise

    inner.__name__, inner.__doc__ = fn.__name__, fn.__doc__
    return inner


def _urls(provider_id: str) -> dict:
    base = get_settings().public_base_url.rstrip("/")
    return {"acs": f"{base}/sso/{provider_id}/acs", "metadata": f"{base}/sso/{provider_id}/metadata", "sls": f"{base}/sso/{provider_id}/logout"}


def settings_dict(provider_id: str, cfg: dict) -> dict:
    u = _urls(provider_id)
    sp = {
        "entityId": cfg.get("sp_entity_id") or u["metadata"],
        "assertionConsumerService": {"url": u["acs"], "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"},
        "NameIDFormat": cfg.get("name_id_format") or "urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified",
        "x509cert": cfg.get("sp_x509_cert") or "",
        "privateKey": cfg.get("sp_private_key") or "",
    }
    idp = {
        "entityId": cfg["idp_entity_id"],
        "singleSignOnService": {"url": cfg["idp_sso_url"], "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"},
        "x509cert": cfg["idp_x509_cert"],
    }
    if cfg.get("idp_slo_url"):
        idp["singleLogoutService"] = {"url": cfg["idp_slo_url"], "binding": "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"}
    signing = bool(sp["privateKey"] and sp["x509cert"])
    return {
        "strict": True,
        "debug": False,
        "sp": sp,
        "idp": idp,
        "security": {
            "authnRequestsSigned": signing,
            "wantAssertionsSigned": bool(cfg.get("want_assertions_signed", True)),
            "wantMessagesSigned": bool(cfg.get("want_messages_signed", False)),
            "wantNameId": True,
            "rejectDeprecatedAlgorithm": True,
            "signatureAlgorithm": "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256",
            "digestAlgorithm": "http://www.w3.org/2001/04/xmlenc#sha256",
        },
    }


def _request_data(path: str, get: dict | None = None, post: dict | None = None) -> dict:
    """python3-saml request description, built from the public base URL (correct behind proxies)."""
    pu = urlparse(get_settings().public_base_url)
    https = pu.scheme == "https"
    return {
        "https": "on" if https else "off",
        "http_host": pu.hostname + (f":{pu.port}" if pu.port else ""),
        "script_name": (pu.path.rstrip("/") + path),
        "get_data": get or {},
        "post_data": post or {},
    }


@_wrap
def metadata_xml(provider_id: str, cfg: dict) -> str:
    _, Settings = _lib()
    s = Settings(settings_dict(provider_id, cfg), sp_validation_only=True)
    xml = s.get_sp_metadata()
    errors = s.validate_metadata(xml)
    if errors:
        raise SAMLError("invalid SP metadata: " + ", ".join(errors))
    return xml.decode() if isinstance(xml, bytes) else xml


@_wrap
def begin_login(provider_id: str, cfg: dict, relay_state: str = "") -> tuple[str, str]:
    """Returns (IdP redirect URL, AuthnRequest ID to check InResponseTo on the way back)."""
    Auth, _ = _lib()
    auth = Auth(_request_data(f"/sso/{provider_id}/login"), settings_dict(provider_id, cfg))
    url = auth.login(return_to=relay_state or None)
    return url, auth.get_last_request_id()


@_wrap
def finish_login(provider_id: str, cfg: dict, form: dict, request_id: str | None) -> Principal:
    Auth, _ = _lib()
    auth = Auth(_request_data(f"/sso/{provider_id}/acs", post=form), settings_dict(provider_id, cfg))
    auth.process_response(request_id=request_id)
    errors = auth.get_errors()
    if errors or not auth.is_authenticated():
        raise SAMLError(f"SAML response rejected: {', '.join(errors) or 'not authenticated'} ({auth.get_last_error_reason()})")
    attrs = auth.get_attributes()

    def first(name: str):
        v = attrs.get(name)
        return v[0] if isinstance(v, list) and v else v

    name_id = auth.get_nameid()
    email = first(cfg.get("email_attribute") or "email") or (name_id if "@" in (name_id or "") else None)
    extra = {}
    for a in cfg.get("attribute_names") or []:
        v = attrs.get(a)
        if v is not None:
            extra[a] = v[0] if isinstance(v, list) and len(v) == 1 else v
    return Principal(
        subject=name_id,
        provider=provider_id,
        email=email,
        name=first(cfg.get("name_attribute") or "displayName"),
        groups=as_list(attrs.get(cfg.get("groups_attribute") or "groups")),
        attributes=extra,
    )
