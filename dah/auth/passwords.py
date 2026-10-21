"""Password hashing with scrypt (stdlib), stored as ``scrypt$n$r$p$salt$hash``."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os

N, R, P, DKLEN = 2**15, 8, 1, 32
MAXMEM = 128 * N * R * 2


def _b64(b: bytes) -> str:
    return base64.b64encode(b).decode()


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P, dklen=DKLEN, maxmem=MAXMEM)
    return f"scrypt${N}${R}${P}${_b64(salt)}${_b64(dk)}"


def verify_password(password: str, stored: str | None) -> bool:
    if not stored or not password:
        return False
    try:
        algo, n, r, p, salt, expected = stored.split("$")
        if algo != "scrypt":
            return False
        n, r, p = int(n), int(r), int(p)
        dk = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=n, r=r, p=p,
                            dklen=len(base64.b64decode(expected)), maxmem=128 * n * r * 2)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(dk, base64.b64decode(expected))


# Used to spend comparable time when the user doesn't exist (no username enumeration by timing).
DUMMY_HASH = hash_password(os.urandom(8).hex())
