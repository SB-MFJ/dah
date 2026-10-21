"""Router registry: each feature contributes an APIRouter."""

from __future__ import annotations

from fastapi import APIRouter


def all_routers() -> list[APIRouter]:
    from . import documents, pipelines

    return [documents.router, pipelines.router]
