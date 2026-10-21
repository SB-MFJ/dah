"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import duckdb
from fastapi import Body, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..config import get_settings
from ..db import engine
from .. import jobs, lake
from .deps import Admin

STATIC = Path(__file__).resolve().parent.parent / "web" / "static"


def _include_routers(app: FastAPI) -> None:
    from . import routes

    for router in routes.all_routers():
        app.include_router(router)


def bootstrap_admin() -> None:
    """Create DAH_BOOTSTRAP_ADMIN_USER as a local admin on first start (no-op once it exists)."""
    s = get_settings()
    if not (s.bootstrap_admin_user and s.bootstrap_admin_password):
        return
    from ..auth.identity import create_local_user, find_user
    from ..db import session_scope

    with session_scope() as db:
        if find_user(db, s.bootstrap_admin_user) is None:
            create_local_user(db, s.bootstrap_admin_user, s.bootstrap_admin_password, is_admin=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    get_settings().ensure_dirs()
    engine()
    bootstrap_admin()
    jobs.start()
    try:
        yield
    finally:
        jobs.stop()


def create_app() -> FastAPI:
    app = FastAPI(title="dah — document lakehouse", version=__version__, lifespan=lifespan)

    @app.exception_handler(duckdb.Error)
    async def _duckdb_error(_req: Request, exc: duckdb.Error):
        return JSONResponse({"detail": f"{type(exc).__name__}: {exc}"}, status_code=400)

    @app.exception_handler(ValueError)
    async def _value_error(_req: Request, exc: ValueError):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.get("/healthz", tags=["system"])
    def healthz():
        return {"ok": True, "version": __version__}

    @app.post("/api/query", tags=["query"], dependencies=[Admin])
    def query(body: dict = Body(..., examples=[{"sql": "SELECT name, mime FROM documents", "limit": 100, "table_versions": {"stats": 2}}])):
        """Run SQL over the lakehouse views. ``table_versions`` pins lake tables to older snapshots (time travel)."""
        con = lake.connect(table_versions={k: int(v) for k, v in (body.get("table_versions") or {}).items()})
        try:
            return lake.run_query(body["sql"], body.get("params"), limit=int(body.get("limit", 1000)), con=con)
        finally:
            con.close()

    _include_routers(app)

    if STATIC.exists():
        app.mount("/static", StaticFiles(directory=STATIC), name="static")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(STATIC / "index.html")
    else:  # pragma: no cover

        @app.get("/", include_in_schema=False)
        def index():
            return RedirectResponse("/docs")

    return app
