"""Router registry: each feature contributes an APIRouter."""

from __future__ import annotations

from fastapi import APIRouter


def all_routers() -> list[APIRouter]:
    from . import documents, pipelines, schema_routes

    return [documents.router, pipelines.router, schema_routes.router]
