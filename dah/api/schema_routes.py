"""Schema / autocomplete API for the editor."""

from __future__ import annotations

from fastapi import APIRouter

from .. import schema
from ..auth.folders import Access
from .deps import Caller, Perm

router = APIRouter(prefix="/api", tags=["schema"], dependencies=[Caller])


@router.get("/schema")
def get_schema(access: Access = Perm):
    """Views + columns, metadata/record keys (with types and samples) and the pipeline ctx/doc API.

    Limited to documents the caller can read (no pipeline tables for non-admins)."""
    return schema.schema(doc_filter=None if access.admin else access.can_read)
