# Feature 04 — Web UI + transformation editor with autofill

Status: COMPLETE

## Tasks
- [x] Schema/completion API: tables, columns, metadata keys, ctx/doc members
- [x] Single-page web UI (documents, query, pipelines, tables; settings/SSO/licenses added by their features)
- [x] Monaco editor (vendored at build time, CDN fallback, textarea fallback)
- [x] Completion provider: `ctx.`, `doc.`, `doc.metadata["…"]`, SQL tables/columns, `metadata->>'…'`
- [x] Query console with result grid
- [x] Tests for completion API and completion logic

## Log
- 2026-10-21: `dah/schema.py` + `GET /api/schema`: every view with columns and types, metadata
  key paths (nested, dotted) with types, frequency and a sample value, record (tabular) keys,
  and `ctx`/`doc` members with signatures and docstrings taken from the real classes by
  introspection, so the editor can't drift from the runtime API.
- 2026-10-21: UI (`dah/web/static`): vanilla JS, no build step. Documents (drag-drop multi-upload
  with path / comment / metadata, path filter, full-text search, detail tabs for text, metadata
  and pages), Query console (Ctrl/Cmd+Enter, schema browser that inserts names), Pipeline editor
  (kind/trigger/output/params, live validation markers, save-as-version, run/backfill, run log
  and history), Tables browser. Extension points (`DocumentPanels`, `PipelinePanels`,
  `TablePanels`) let later features add panels.
- 2026-10-21: `editor.js` autofill:
  - Python: `ctx.` members; `doc.` members for `doc`, loop variables from `for x in ctx.documents()`,
    `x = ctx.document()`, and the transform's first parameter; `doc.metadata["a"]["…` and
    `.get("a", {}).get("…` complete nested metadata keys with type, frequency and sample;
    `ctx.read_table("…`/`write_table("…` complete table names; SQL completion inside
    `ctx.sql("…")`/`df`/`arrow`/`documents("…")` strings; hover docs; snippets.
  - SQL: tables after FROM/JOIN, `alias.` columns, columns of referenced tables,
    `metadata->'a'->>'…` nested keys, `data->>'…` record keys, keywords, JSON hints.
- 2026-10-21: Tests: schema API test plus a Node test (`tests/js/completions.test.js`) that runs the
  real `editor.js` providers against a fake Monaco model (16 assertions). Live server smoke test
  with curl passed.
- 2026-10-21: Fixed a bug found in the smoke test: files starting with `[section]` (toml/ini) were
  detected as JSON.
- Not verified here: rendering in a real browser (no browser run during the build). The JS passes
  `node --check` and the completion logic is unit-tested.
