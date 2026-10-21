"""Schema / autocomplete API for the editor."""

from __future__ import annotations

from fastapi import APIRouter

from .. import schema
from .deps import Admin

router = APIRouter(prefix="/api", tags=["schema"], dependencies=[Admin])


@router.get("/schema")
def get_schema():
    """Views + columns, metadata/record keys (with types and samples) and the pipeline ctx/doc API."""
    return schema.schema()
