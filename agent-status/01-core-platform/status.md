# Feature 01 — Core platform (Docker, config, catalog, blob storage, API shell)

Status: COMPLETE

## Tasks
- [x] Python package layout (`dah/`), `pyproject.toml`, dependency pins
- [x] Settings via environment (`DAH_*`), `.env.example`
- [x] Catalog database (SQLAlchemy; SQLite default, Postgres via `DAH_DATABASE_URL`)
- [x] Content-addressed blob store (raw zone)
- [x] Lakehouse query layer (DuckDB over catalog + Parquet zones)
- [x] FastAPI app shell with admin-token auth and health endpoint
- [x] Dockerfile + docker-compose (app, mcp, optional postgres)
- [x] Test harness (pytest) + smoke tests

## Log
- 2026-10-21: Package skeleton, settings, catalog models (documents, pipelines, lake tables,
  settings, identity providers, data endpoints, access log), all with version tables.
- 2026-10-21: Blob store (sha256 content addressing, dedup, read-only files).
- 2026-10-21: `dah/lake.py` builds a per-query in-memory DuckDB with views `documents`,
  `document_versions`, `pages`, `records` + one view per pipeline table; time-travel pins;
  DuckDB external access restricted to the lake dir and config locked (verified by test).
- 2026-10-21: FastAPI shell, admin bearer auth, `/healthz`, `/api/query`, background job runner.
- 2026-10-21: Dockerfile (tesseract, xmlsec, LibreOffice, vendored Monaco, non-root user) and
  docker-compose (app + mcp + optional postgres profile).
- 2026-10-21: 5 tests passing. Note: Docker daemon was not running on the build machine, so the
  image itself has not been built here yet; local verification uses a venv.
