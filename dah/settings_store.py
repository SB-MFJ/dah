"""Runtime settings kept in the catalog. Secrets are encrypted with a key derived from DAH_SECRET_KEY."""

from __future__ import annotations

import base64
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .config import get_settings
from .db import Setting, session_scope


@lru_cache
def _fernet(secret: str) -> Fernet:
    key = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"dah-settings-v1", info=b"settings-encryption").derive(secret.encode())
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt(value: str) -> str:
    return _fernet(get_settings().secret_key).encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet(get_settings().secret_key).decrypt(token.encode()).decode()
    except InvalidToken as e:
        raise RuntimeError("cannot decrypt secret setting; was DAH_SECRET_KEY changed?") from e


def get(key: str, default: str | None = None) -> str | None:
    with session_scope() as s:
        row = s.get(Setting, key)
        if row is None:
            return default
        return decrypt(row.value) if row.encrypted else row.value


def put(key: str, value: str | None, secret: bool = False) -> None:
    with session_scope() as s:
        row = s.get(Setting, key)
        if value is None:
            if row is not None:
                s.delete(row)
            return
        stored = encrypt(value) if secret else value
        if row is None:
            s.add(Setting(key=key, value=stored, encrypted=secret))
        else:
            row.value, row.encrypted = stored, secret


def hint(secret: str | None) -> str | None:
    if not secret:
        return None
    return "…" + secret[-4:] if len(secret) > 8 else "set"
