"""Shared FastAPI dependencies: database session and caller identity / roles."""

from __future__ import annotations

from typing import Iterator

from fastapi import Depends, HTTPException, Query, Request, status
from sqlalchemy.orm import Session

from ..auth.identity import AuthError, Identity, authenticate
from ..db import session_scope


def get_db() -> Iterator[Session]:
    with session_scope() as s:
        yield s


def optional_identity(request: Request, db: Session = Depends(get_db)) -> Identity | None:
    try:
        return authenticate(request, db)
    except AuthError as e:
        raise HTTPException(e.status, str(e), headers={"WWW-Authenticate": "Bearer"} if e.status == 401 else None)


def current_identity(ident: Identity | None = Depends(optional_identity)) -> Identity:
    if ident is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "authentication required", headers={"WWW-Authenticate": "Bearer"})
    return ident


def require_admin(ident: Identity = Depends(current_identity)) -> Identity:
    if not ident.is_admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "administrator access required")
    return ident


def require_account(ident: Identity = Depends(current_identity)) -> Identity:
    """Caller must be a user account (not the admin token or an IdP-only principal)."""
    if ident.user_id is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "this action needs a user account (sign in as a user)")
    return ident


def get_access(ident: Identity = Depends(current_identity), db: Session = Depends(get_db)):
    """Folder permissions of the caller."""
    from ..auth.folders import load_access

    return load_access(db, ident)


Admin = Depends(require_admin)
Perm = Depends(get_access)
Caller = Depends(current_identity)
DB = Depends(get_db)


def pagination(limit: int = Query(100, ge=1, le=10000), offset: int = Query(0, ge=0)) -> tuple[int, int]:
    return limit, offset
