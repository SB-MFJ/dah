# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

dah is a self-hosted document lakehouse (Python 3.11+, FastAPI, SQLAlchemy catalog, DuckDB over Parquet). README.md is the user-facing feature and configuration reference; this file covers how the code fits together.

## Commands

```bash
python -m venv .venv && .venv/bin/pip install -e '.[saml,dev]'   # add ,postgres for the Postgres catalog
DAH_ADMIN_TOKEN=dev .venv/bin/dah serve --port 8000              # API + UI
.venv/bin/dah mcp --transport stdio                              # MCP server (talks to the API over HTTP)

.venv/bin/pytest                                                 # full suite (120s per-test timeout)
.venv/bin/pytest tests/test_folders.py::test_name -x             # single test
node tests/js/completions.test.js dah/web/static                 # editor autocomplete tests (also run via tests/test_ui.py)

.venv/bin/dah licenses --markdown > THIRD_PARTY_LICENSES.md      # regenerate after changing dependencies
docker compose up -d --build                                     # app :8000, MCP :8001
```

No linter or formatter is configured. The OCR test is skipped without `tesseract`, legacy Office conversion needs `soffice`, and the JS test is skipped without `node`. `tests/test_licenses.py` fails if a new dependency isn't open source.

## Architecture

**Request flow.** `dah/api/app.py:create_app` registers every router listed in `dah/api/routes.py:all_routers` (add new routers there). `/api/query` lives directly in `app.py`. Auth is done with dependencies from `dah/api/deps.py`: `Caller` (any `Identity`), `Admin` (admin only), `Perm` (an `auth.folders.Access` for the caller), `DB` (a session from `db.session_scope`, which commits on success).

**Identity.** `auth/identity.py:authenticate` resolves one `Identity` from the `DAH_ADMIN_TOKEN` bearer, a personal API token (`dah_pat_…`, stored hashed), a signed session cookie/bearer, or an IdP's own JWT. Cookie-authenticated writes require the `X-DAH-CSRF: 1` header. External sign-ins (OIDC/SAML in `sso/`, LDAP in `auth/ldap.py`) all go through `provision_external`, which creates the `User` and refreshes IdP groups. `sso/` also holds the separate `/data/{slug}` endpoint feature (`sso/acl.py` policies: row filters from claims, column groups).

**Folder permissions are enforced by the caller, not centrally.** Folders are derived from document paths (`finance/2026/q1.pdf` is in `finance/2026`; the root is `""`). `Access` exposes `can(path, level)`, `sql_clause(column)` for SQLAlchemy and `duck_clause(column, params)` for DuckDB. Admins bypass all checks. Any new code path that returns documents must apply one of these. Unreadable documents must 404, not 403 (see `get_doc` in `api/documents.py`). Pipelines, tables, settings, providers and users are admin-only.

**Catalog vs lake.** `db.py` holds all SQLAlchemy models; the schema is built with `create_all` (no migrations), so new columns on existing tables won't appear in an existing database. Raw bytes are content-addressed blobs (`storage.py`). Extraction writes per-version bronze Parquet (`lake.write_pages` / `write_records`). Pipeline outputs are immutable `lake/tables/<name>/v<N>.parquet` snapshots (`lake.commit_table_snapshot`).

**SQL.** `lake.connect()` builds a fresh in-memory DuckDB connection per query: catalog rows are registered as Arrow tables behind the `documents`/`document_versions` views, `pages`/`records` are views over bronze Parquet, plus one view per pipeline table. The connection is then locked (`enable_external_access=false`, `lock_configuration=true`). With `doc_filter` (non-admins), pipeline tables are omitted and `allowed_paths` is restricted to the exact Parquet files of readable documents, so `read_parquet()` can't bypass the filter. Always close connections you open.

**Background work.** `jobs.py` is an in-process thread pool started in the app lifespan. When no app is running (CLI, most tests), `jobs.submit` runs **synchronously**. Job state lives in catalog `status` columns, and `@jobs.on_startup` hooks (e.g. `ingest.service.recover`) re-queue in-flight work after a restart. Ingestion: `ingest_file` → `process_version` (detect → extract/OCR → bronze → `pipelines.service.on_ingest` hooks).

**Pipelines** run in a separate `python -I -m dah.pipelines.worker` subprocess with time/memory limits and secrets stripped from the environment (`SECRET_ENV` in `pipelines/service.py`). The worker talks to the catalog and lake directly; `pipelines/context.py` defines the `ctx`/`doc` API. If you change that API, update the completions served by `schema.py` / `api/schema_routes.py` and `web/static/editor.js` too.

**Versioning** (`versioning.py`, `api/versions.py`) covers documents, pipelines and tables. A restore appends a copy of an old version as the new current version; history is never rewritten.

**MCP** (`mcp_server.py`) is a thin client of the REST API over `httpx`. It forwards the MCP caller's bearer token, so the API enforces that user's role and folders. New MCP tools should call existing endpoints rather than touch the catalog directly.

**Web UI** (`dah/web/static/`) is plain JS with no build step. Monaco is vendored only in the Docker image.

**Settings** come from `DAH_*` env vars via `config.get_settings()` (cached with `lru_cache`). Runtime-editable settings (the ML assistant key, etc.) live in the catalog via `settings_store.py`, encrypted with a key derived from `DAH_SECRET_KEY`.

## Tests

`tests/conftest.py`: `env` points `DAH_DATA_DIR` at a tmp dir and resets the cached settings, engine and login throttle; `client` is a `TestClient` authenticated with the admin token; `anon` has no credentials. Test documents (PDF, DOCX, XLSX, …) are generated in memory by `tests/fixtures.py`. If you add module-level caches, reset them in `env`.

## Project conventions

- `agent-status/<NN-feature>/status.md` holds the task list and a dated progress log for each feature, and `agent-status/README.md` indexes them. Add or update one when working on a feature.
- Each finished feature is one commit (`feat(scope): …`).
- When a feature changes behavior or configuration, update README.md's feature sections and config table.
