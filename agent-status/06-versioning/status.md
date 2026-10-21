# Feature 06 — Versioning of files, pipelines and lake tables

Status: COMPLETE

## Tasks
- [x] Document versions (re-upload to same path => new version)
- [x] List / download / view any prior document version
- [x] Restore prior document version as current (non-destructive, new version)
- [x] Pipeline versions on every save, diff between versions
- [x] Restore prior pipeline version as current
- [x] Lake table snapshots + restore (time travel reads)
- [x] UI for version history + restore
- [x] Tests

## Log
- 2026-10-21: Version tables were part of the core schema (feature 01); uploads to an existing
  path append versions (feature 02); pipeline saves append versions only when something changed
  (feature 03).
- 2026-10-21: `dah/versioning.py` — restore copies the chosen version into a new version with
  `restored_from` set (documents reuse the immutable blob + bronze files; tables copy the
  Parquet snapshot), so history is never rewritten and a restore can be undone. Unified text/code
  diffs and metadata/field diffs.
- 2026-10-21: API: `GET /api/documents/{id}/versions[/{v}]`, `GET …/diff?a=&b=`,
  `POST …/versions/{v}/restore`; same for `/api/pipelines/{ref}/…` and
  `/api/tables/{name}/versions[/{v}/restore]`. `POST /api/query` takes `table_versions` for time
  travel; `POST /api/pipelines/{ref}/run` takes `version` to run an old version without restoring.
- 2026-10-21: UI: "Versions" tab on documents (view, diff, download, restore with confirm + comment),
  version history under the pipeline editor (diff vs editor, load into editor, restore), and
  table snapshots with preview + restore.
- 2026-10-21: 3 versioning tests pass (28 total).
