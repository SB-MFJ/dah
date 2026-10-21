# Feature 11 — Per-folder permissions

Status: COMPLETE

## Tasks
- [x] Folder grants: path prefix × (user | group | authenticated) × (read | write | manage), inherited by subfolders
- [x] Enforcement: document list/get/text/pages/download/search/versions/restore/upload/reprocess/delete
- [x] Enforcement in SQL: non-admin queries see only readable documents/pages/records
- [x] Schema/autofill limited to readable data
- [x] Folder tree + grant management API (admins and folder managers)
- [x] UI: folders view with grants editor, effective access check
- [x] Tests

## Log
- 2026-10-21: `auth/folders.py` — the effective level is the highest matching grant on the folder or
  any ancestor; group grants match local and IdP groups; user grants match username or email;
  admins bypass. Helpers produce SQLAlchemy and DuckDB (parameterised) filters from the
  minimal set of readable root folders.
- 2026-10-21: Documents API: a document that isn't readable returns 404 (same as missing), and
  read-but-not-allowed returns 403. Upload/restore/reprocess need write; delete needs manage.
  Multi-file uploads are checked in full before anything is stored. Versions record the acting
  user.
- 2026-10-21: SQL: non-admins always get a filtered connection (document views filtered; no
  pipeline tables) where DuckDB `allowed_paths` lists only the exact Parquet files of readable
  documents, with external access disabled and config locked. `read_parquet`/glob/COPY/SET
  bypass attempts fail (tested, plus checked on a live server). Search, schema/autofill
  metadata keys and MCP all use the same filtering. Internal `_`-prefixed helper views are
  hidden from autofill.
- 2026-10-21: `/api/folders` (tree with document counts and your level), `/api/folders/grants`
  (list direct / inherited / below; upsert; delete; needs manage on the folder),
  `/api/folders/effective` (a user's level and the grants that give it). Data endpoints can opt
  in to folder filtering with `"folder_column": "path"` (fails closed if the column is missing).
- 2026-10-21: UI: "Folders" view for everyone (tree, documents, your level); managers/admins get
  the grant editor (group autocomplete, inherited and subfolder grants, removal with
  confirmation) and the access checker.
- 2026-10-21: Bug found and fixed: `PurePosixPath("//x")` keeps `//` as a distinct root, so
  document paths uploaded with a leading slash were stored as `///a/b.txt`, and a `/` folder
  grant became `//`. Both normalizers are now split-based, with a regression test. MCP
  tool failures now use `ToolError`, so clients see the reason (e.g. 403).
- 2026-10-21: 9 folder tests + 1 normalization test pass (78 total).
