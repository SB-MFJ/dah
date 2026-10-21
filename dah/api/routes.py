"""Router registry: each feature contributes an APIRouter."""

from __future__ import annotations

from fastapi import APIRouter


def all_routers() -> list[APIRouter]:
    from . import assist_routes, auth_routes, documents, license_routes, pipelines, schema_routes, sso_routes, versions

    return [
        documents.router,
        pipelines.router,
        schema_routes.router,
        versions.router,
        assist_routes.router,
        sso_routes.admin,
        sso_routes.public,
        license_routes.router,
        auth_routes.auth,
        auth_routes.users,
    ]
