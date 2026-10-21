"""Shared FastAPI dependencies."""

from __future__ import annotations

import hmac
from typing import Iterator

from fastapi import Depends, Header, HTTPException, Query, status
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import session_scope


def get_db() -> Iterator[Session]:
    with session_scope() as s:
        yield s


def require_admin(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None),
) -> str:
    """Admin API auth: ``Authorization: Bearer <DAH_ADMIN_TOKEN>`` or ``X-API-Key``."""
    token = x_api_key
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    expected = get_settings().admin_token
    if not token or not hmac.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "admin token required", headers={"WWW-Authenticate": "Bearer"})
    return "admin"


Admin = Depends(require_admin)
DB = Depends(get_db)


def pagination(limit: int = Query(100, ge=1, le=10000), offset: int = Query(0, ge=0)) -> tuple[int, int]:
    return limit, offset
