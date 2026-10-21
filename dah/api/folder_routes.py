"""Folder tree and per-folder permission grants."""

from __future__ import annotations

from collections import Counter
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..auth.folders import LEVELS, Access, ancestors, covers, grant_dict, load_access, norm_folder
from ..auth.identity import find_user, identity_for_user
from ..db import Document, FolderGrant, User
from .deps import DB, Caller, Perm

router = APIRouter(prefix="/api/folders", tags=["folders"], dependencies=[Caller])


class GrantIn(BaseModel):
    path: str = ""
    principal_type: Literal["user", "group", "authenticated"]
    principal: str = ""
    level: Literal["read", "write", "manage"]


def _need_manage(access: Access, path: str) -> None:
    if not access.can(path, "manage"):
        raise HTTPException(403, "manage permission required on this folder")


@router.get("")
def folder_tree(db: Session = DB, access: Access = Perm):
    """Folders the caller can see, with nested document counts and the caller's level on each."""
    stmt = select(Document.path).where(Document.current_version > 0)
    clause = access.sql_clause(Document.path)
    if clause is not None:
        stmt = stmt.where(clause)
    counts: Counter = Counter()
    for (path,) in db.execute(stmt):
        for a in ancestors(path):
            counts[a] += 1
    # Granted folders show up even when empty; their parents are listed for navigation.
    grant_paths = {g.path for g in (db.scalars(select(FolderGrant)) if access.admin else access.grants)}
    paths = set(counts) | {""}
    for gp in grant_paths:
        paths.add(gp)
        paths.update(ancestors(gp + "/x"))
    items = [{"path": p, "documents": counts.get(p, 0), "my_level": next((k for k, v in LEVELS.items() if v == access.level(p)), None)}
             for p in sorted(paths)]
    return {"items": items, "is_admin": access.admin}


@router.get("/grants")
def list_grants(path: str = "", db: Session = DB, access: Access = Perm):
    """Grants on a folder and those inherited from its ancestors (needs manage on the folder)."""
    path = norm_folder(path)
    _need_manage(access, path)
    rows = db.scalars(select(FolderGrant).order_by(FolderGrant.path, FolderGrant.principal_type, FolderGrant.principal)).all()
    return {
        "path": path,
        "grants": [grant_dict(g) for g in rows if g.path == path],
        "inherited": [grant_dict(g) for g in rows if g.path != path and covers(g.path, path)],
        "below": [grant_dict(g) for g in rows if g.path != path and covers(path, g.path)],
    }


@router.post("/grants", status_code=201)
def upsert_grant(body: GrantIn, db: Session = DB, access: Access = Perm):
    path = norm_folder(body.path)
    _need_manage(access, path)
    principal = "" if body.principal_type == "authenticated" else body.principal.strip()
    if body.principal_type != "authenticated" and not principal:
        raise HTTPException(400, "principal is required for user and group grants")
    g = db.scalar(select(FolderGrant).where(FolderGrant.path == path, FolderGrant.principal_type == body.principal_type,
                                            FolderGrant.principal == principal))
    if g is None:
        g = FolderGrant(path=path, principal_type=body.principal_type, principal=principal, level=body.level, created_by=access.ident.username)
        db.add(g)
    else:
        g.level = body.level
    db.flush()
    return grant_dict(g)


@router.delete("/grants/{grant_id}")
def delete_grant(grant_id: int, db: Session = DB, access: Access = Perm):
    g = db.get(FolderGrant, grant_id)
    if g is None:
        raise HTTPException(404, "grant not found")
    _need_manage(access, g.path)
    db.delete(g)
    return {"deleted": grant_id}


@router.get("/effective")
def effective(path: str, user: str, db: Session = DB, access: Access = Perm):
    """What level does ``user`` (username or email) have on ``path``? Needs manage on the path."""
    path = norm_folder(path)
    _need_manage(access, path)
    u = find_user(db, user) or db.scalar(select(User).where(User.email == user))
    if u is None:
        raise HTTPException(404, "user not found")
    ua = load_access(db, identity_for_user(u, "check"))
    lvl = ua.level(path)
    return {"user": u.username, "path": path, "level": next((k for k, v in LEVELS.items() if v == lvl), None),
            "admin": ua.admin, "via": [grant_dict(g) for g in ua.grants if covers(g.path, path)]}
