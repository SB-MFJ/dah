# Feature 03 — Custom transformations & processing pipelines (Python + SQL)

Status: COMPLETE

## Tasks
- [x] Pipeline model: kind (python|sql), trigger (manual|on_ingest), output table
- [x] Subprocess worker with timeout / resource limits
- [x] `ctx` API: sql, documents, read_table, write_table, log, params
- [x] SQL pipelines materialized to Parquet tables (silver/gold zone)
- [x] On-ingest transforms that enrich document metadata
- [x] Run history with logs
- [x] CRUD + run API
- [x] Tests

## Log
- 2026-10-21: `pipelines/context.py` — `ctx` (sql/df/arrow/documents/document/read_table/
  write_table/log/params) and `Doc` (id, path, text, pages, records, metadata, open(), blob_path).
- 2026-10-21: `pipelines/worker.py` — runs each job in `python -I -m dah.pipelines.worker`;
  CPU rlimit (+ address-space limit on Linux), wall-clock timeout in the parent, admin token /
  secret key / provider API keys stripped from the environment (verified by test).
- 2026-10-21: `pipelines/service.py` — create / save-new-version / run; outputs committed as new
  lake table snapshots (replace or append); on_ingest transforms registered as a post-ingest
  hook; returned dicts merged into document metadata (ingest keys like `mime`/`sha256` protected);
  backfill applies a transform to all current documents; static validation for the editor
  (python AST / DuckDB EXPLAIN).
- 2026-10-21: `/api/pipelines` CRUD, run (sync/async), backfill, runs, templates, validate;
  `/api/tables`.
- 2026-10-21: Fixes along the way: DuckDB 2.0 renamed Arrow fetch to `to_arrow_table`; added `pytz`
  (DuckDB needs it for tz-aware timestamps); empty outputs keep the previous schema.
- 2026-10-21: 7 pipeline tests pass (21 total).
- Note for users: in DuckDB `->>` binds looser than `=`, so write `(metadata->>'k') = 'v'`.
