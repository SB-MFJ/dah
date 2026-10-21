"""Per-folder permissions.

A grant gives a principal a level on a folder path; it applies to that folder, every subfolder
and every document beneath it ("" is the root). Effective level = the highest matching grant.

  principal_type  user (username or email) | group (local or IdP group) | authenticated (any signed-in user)
  level           read (list / view / download / query) < write (+ upload, new versions, restore, reprocess)
                  < manage (+ delete documents, manage grants under the folder)

Administrators bypass folder checks.
"""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..db import FolderGrant
from .identity import Identity

LEVELS = {"read": 1, "write": 2, "manage": 3}
PRINCIPAL_TYPES = ("user", "group", "authenticated")


def norm_folder(path: str | None) -> str:
    """'/a//b/' -> 'a/b'; '' or '/' -> '' (root)."""
    return "/".join(x for x in (path or "").replace("\\", "/").split("/") if x not in ("", ".", ".."))


def covers(folder: str, path: str) -> bool:
    return folder == "" or path == folder or path.startswith(folder + "/")


def ancestors(path: str) -> list[str]:
    """'a/b/c.pdf' -> ['', 'a', 'a/b']"""
    parts = path.split("/")[:-1]
    return [""] + ["/".join(parts[: i + 1]) for i in range(len(parts))]


class Access:
    def __init__(self, ident: Identity, grants: list[FolderGrant]):
        self.ident = ident
        self.admin = ident.is_admin
        names = {ident.username.lower()} | ({ident.email.lower()} if ident.email else set())
        groups = set(ident.groups)

        def applies(g: FolderGrant) -> bool:
            if g.principal_type == "authenticated":
                return True
            if g.principal_type == "group":
                return g.principal in groups
            return g.principal_type == "user" and g.principal.lower() in names

        self.grants = [g for g in grants if applies(g)]

    def level(self, path: str) -> int:
        if self.admin:
            return LEVELS["manage"]
        return max((LEVELS[g.level] for g in self.grants if covers(g.path, path)), default=0)

    def can(self, path: str, need: str = "read") -> bool:
        return self.level(path) >= LEVELS[need]

    def can_read(self, path: str) -> bool:
        return self.can(path, "read")

    def read_roots(self) -> list[str] | None:
        """Minimal set of folders the caller can read; None means everything."""
        if self.admin:
            return None
        roots = sorted({g.path for g in self.grants if LEVELS[g.level] >= 1}, key=len)
        if "" in roots:
            return None
        kept: list[str] = []
        for r in roots:
            if not any(covers(k, r) for k in kept):
                kept.append(r)
        return kept

    def sql_clause(self, column):
        """SQLAlchemy filter for readable paths (None = no filter)."""
        roots = self.read_roots()
        if roots is None:
            return None
        if not roots:
            return column == "\x00never"
        return or_(*[or_(column == r, column.startswith(r + "/", autoescape=True)) for r in roots])

    def duck_clause(self, column: str, params: list) -> str | None:
        """DuckDB SQL filter with bound parameters (None = no filter)."""
        roots = self.read_roots()
        if roots is None:
            return None
        if not roots:
            return "FALSE"
        parts = []
        for r in roots:
            params.append(r)
            n = len(params)
            parts.append(f"({column} = ${n} OR starts_with({column}, ${n} || '/'))")
        return "(" + " OR ".join(parts) + ")"


def load_access(db: Session, ident: Identity) -> Access:
    grants = [] if ident.is_admin else list(db.scalars(select(FolderGrant)))
    return Access(ident, grants)


def grant_dict(g: FolderGrant) -> dict:
    return {"id": g.id, "path": g.path, "principal_type": g.principal_type, "principal": g.principal, "level": g.level,
            "created_by": g.created_by, "created_at": g.created_at}
