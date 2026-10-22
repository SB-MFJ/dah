# Pre word

> DISCLAIMER:
> This is a proof of concept and a test of claude 5.5 
> DO NOT USE THIS IN PRODUCTION

The model was prompted with:
```
build a self host docker based data lake house system in cpython:

it needs to be able to injest documents of any type (doc, pdf, png, txt)
generate metadata about it and convert it to queryable data with ocr or document transcription as needed
allow for custom transformations and data processing pipelines with python
the transformations editor should leverage autofill for data objects and metadata fields
allow for an mcp server and a configurable ML api key for editing pipeline queries
have it be versioned for files and pipeline queries and allow for retrieval of prior versions and replacement of current version with a previous one
tools and libraries used should be opensource and have a license ackgnowledgement page
lastly it needs a way to configure multipe saml/sso authenticated api endpoint that allows for data to be read with access control.

for every feature, have a list of tasks in a readme file that you write to after completing a portion of a feature as a status update in a agent-status/feature/status.md sub directory. commit every complete feature to local git.
do not read any files on this system outside of the folder we are in.
```
This was the result with no manual intervention.

# dah — self-hosted document lakehouse

A concise, common Ukrainian equivalent for the upper covering of a building. AKA roof.

dah ingests documents of any type, extracts their text and metadata (with OCR where needed), and
makes everything queryable with SQL. You can transform the data with versioned Python or SQL
pipelines, edit those pipelines with an autocompleting editor or an AI assistant, and reach the
lakehouse from MCP clients. Users sign in with local accounts or through OIDC, SAML or LDAP, and
per-folder permissions decide which documents each user can see and change. You can also publish
datasets behind SSO with row- and column-level access control.

Everything runs in Docker, is written in Python (CPython 3.11+), and uses only open-source
components (see [`/licenses`](THIRD_PARTY_LICENSES.md)). dah itself is licensed under the
[Apache License 2.0](LICENSE).

## Quick start

```bash
cp .env.example .env          # set DAH_ADMIN_TOKEN, DAH_SECRET_KEY, DAH_PUBLIC_BASE_URL,
                              #     DAH_BOOTSTRAP_ADMIN_USER / DAH_BOOTSTRAP_ADMIN_PASSWORD
docker compose up -d --build  # app on :8000, MCP server on :8001
open http://localhost:8000    # sign in as the bootstrap admin
```

Optional Postgres catalog: `docker compose --profile postgres up -d` and set
`DAH_DATABASE_URL=postgresql+psycopg://dah:dah@postgres:5432/dah`.

Without Docker (OCR needs `tesseract`; legacy Office formats need LibreOffice's `soffice`):

```bash
python -m venv .venv && .venv/bin/pip install -e '.[saml,dev]'
DAH_ADMIN_TOKEN=dev .venv/bin/dah serve --port 8000
.venv/bin/pytest
```

## Architecture

```
            ┌──────────── web UI (Monaco editor, autofill) ────────────┐
upload ──▶  │ FastAPI app                                              │ ◀── MCP server (stdio / HTTP :8001)
            │  ingest ─▶ extract/OCR ─▶ bronze Parquet ─▶ hooks        │
            │  pipelines (subprocess sandbox) ─▶ table snapshots        │ ◀── /data/{slug} (SAML / OIDC, ACL)
            │  DuckDB per query over catalog + Parquet (read-only)      │
            └───────────────┬──────────────────────────────┬───────────┘
                    catalog (SQLite/Postgres)        /data volume
                    versions, settings, SSO          blobs/ (raw, content-addressed)
                                                     lake/bronze/ (pages, records)
                                                     lake/tables/<name>/v<N>.parquet
```

| Zone | What | Where |
|---|---|---|
| raw | original bytes, deduplicated by SHA-256, read-only | `data/blobs/` |
| bronze | extracted pages + tabular records per document version | `data/lake/bronze/<doc>/v<N>/` |
| tables | pipeline outputs, one Parquet snapshot per version | `data/lake/tables/<name>/` |
| catalog | documents, versions, pipelines, runs, settings, SSO config, audit log | SQLite or Postgres |

### SQL views

Every query gets a fresh in-memory DuckDB connection. It can read only the lake directory, and
its configuration is locked.

| View | Columns |
|---|---|
| `documents` | current version: `id, path, name, version, filename, mime, size, sha256, status, page_count, text, metadata (JSON), created_at, version_created_at` |
| `document_versions` | every version (same columns + `is_current`) |
| `pages` | `document_id, version, page, text, ocr` |
| `records` | rows of spreadsheets/CSV/JSON arrays/DOCX tables: `document_id, version, sheet, row_number, data (JSON)` |
| `<table>` | latest snapshot of each pipeline output table |

```sql
SELECT path, metadata->>'author' AS author, page_count
FROM documents
WHERE (metadata->>'extraction_method') = 'pdf+ocr';   -- parenthesise: ->> binds looser than =
```

## Features

### 1. Ingestion, metadata and OCR

`POST /api/documents` (multipart, many files) or drag-and-drop in the UI, or
`dah ingest <files…> --prefix folder/`.

**Virus scanning.** Set `DAH_CLAMAV_URL` (`tcp://host:3310` or `unix:///path/to/clamd.sock`) and
every upload is streamed to ClamAV's `clamd` before it is stored. An infected file is rejected
with `422` and never reaches the blob store. In a multi-file upload, all files are scanned
first, so one infected file rejects the whole request. If clamd can't give a verdict
(unreachable, timeout, file larger than clamd's `StreamMaxLength`, 100 MB by default), the upload
fails with `503`, unless `DAH_CLAMAV_FAIL_OPEN=true`. `dah ingest` skips infected files and exits
with status 1. Raise clamd's `StreamMaxLength` to match `DAH_MAX_UPLOAD_MB`. With Docker:
`docker compose --profile clamav up -d` and `DAH_CLAMAV_URL=tcp://clamav:3310` in `.env`.

| Type | Extraction |
|---|---|
| PDF | text layer per page; pages with less than `DAH_OCR_MIN_PAGE_CHARS` characters are rendered at 300 dpi and OCR'd |
| Images (png, jpg, tiff, gif, webp, bmp…) | Tesseract OCR (multi-frame TIFF supported), dimensions, DPI, EXIF |
| DOCX / PPTX / XLSX | paragraphs, headers/footers, tables, slides + notes, sheets; core properties |
| DOC / XLS / PPT / RTF / ODT / ODS / ODP / WPD | converted with headless LibreOffice, then extracted as above |
| CSV / TSV / JSON / NDJSON | text + rows in `records` |
| TXT / MD / HTML / XML / YAML / EML | decoded text (charset detection), HTML stripped, email headers |
| anything else | stored with generic metadata |

Every version gets metadata such as `filename, extension, mime, size, sha256, page_count,
char_count, word_count, ocr_pages, ocr_confidence, extraction_method, ingested_at`, plus the
format's own fields (`title, author, created, modified, …`) and any user metadata JSON under
`user`.

### 2. Pipelines (custom transformations)

Pipelines are Python or SQL, run manually or on every ingest. Each run executes in a separate
`python -I` process with a timeout and CPU/memory limits. Secrets are removed from its
environment.

```python
def run(ctx):                                   # trigger: manual
    rows = []
    for doc in ctx.documents("mime = 'application/pdf'"):
        rows.append({"path": doc.path, "author": doc.metadata.get("author"), "pages": len(doc.pages)})
    return rows                                 # -> output table snapshot

def transform(doc, ctx):                        # trigger: on_ingest (+ "Backfill" for existing docs)
    return {"invoice_total": find_total(doc.text)}   # merged into doc.metadata
```

`ctx`: `sql()`, `df()`, `arrow()`, `documents()`, `document()`, `read_table(name, version)`,
`write_table(name, rows, mode="replace"|"append")`, `log()`, `params`.
`doc`: `id, path, name, version, mime, size, sha256, text, pages, records, metadata, open(), blob_path`.

### 3. Editor with autofill

The pipeline editor and SQL console use Monaco. Completions come from `GET /api/schema`, which
reads the live data:

- `ctx.` and `doc.` members with signatures and docs. Loop variables such as
  `for d in ctx.documents()` and the transform's parameter are recognised as documents.
- `doc.metadata["…"]` and `.get("a", {}).get("…")` complete metadata keys at any nesting depth,
  showing type, how many documents have the key, and a sample value.
- SQL inside `ctx.sql("…")` strings, table names in `read_table("…")`.
- SQL: tables after `FROM`/`JOIN`, `alias.` columns, `metadata->'a'->>'…'`, `data->>'…'`.
- Live validation markers (Python syntax, DuckDB binding errors).

### 4. MCP server and ML assistant

**ML key**: under Settings → ML assistant, pick a provider (Anthropic by default, model
`claude-opus-5-5`, or any OpenAI-compatible server such as Ollama or vLLM), then set the model,
effort and API key. The key is encrypted at rest and never returned by the API. In the pipeline
editor, "Ask AI" sends the current code, your instruction and the live schema, and shows the
proposal as a validated diff. Nothing is saved until you click Save. With Anthropic, server-side
fallback on declined requests (a beta feature) is on by default; you can turn it off in settings.

**MCP**: each client authenticates with its own personal API token and acts with that user's role
and folder permissions. 16 tools: `list_documents, get_document, search_text, run_sql, describe_schema,
list_tables, list_pipelines, get_pipeline, validate_pipeline, create_pipeline, update_pipeline,
run_pipeline, assist_edit_pipeline, list_versions, diff_versions, restore_version`.

```jsonc
// streamable HTTP (docker compose service "mcp")
{ "url": "http://localhost:8001/mcp", "headers": { "Authorization": "Bearer <personal API token>" } }
// stdio
{ "command": "dah-mcp", "env": { "DAH_API_URL": "http://localhost:8000", "DAH_MCP_TOKEN": "<personal API token>" } }
```

### 5. Versioning

- **Files**: uploading to an existing path creates a new version. Re-uploading identical bytes
  is a no-op.
- **Pipelines**: each save that changes something creates a new version, and you can run any
  old version.
- **Tables**: each pipeline run writes a new snapshot. Query old snapshots with
  `{"table_versions": {"t": 3}}`.

All three support history, diff, download/view and **restore**. Restore appends a copy of the
chosen version as the new current version, so history is never lost and a restore can itself be
undone.

### 6. SSO-protected data API with access control

Add any number of **SAML 2.0** and **OIDC** identity providers (Access → Identity providers),
then publish **data endpoints**. Each endpoint has a source (a lake table/view or a SQL query)
and a policy:

```json
{
  "providers": ["okta", "corp-adfs"],
  "allowed_groups": ["finance"],
  "allowed_users": ["cfo@example.com"],
  "row_filters": [{"column": "region", "claim": "groups", "op": "in"},
                  {"column": "department", "claim": "attributes.department"}],
  "unrestricted_groups": ["auditors"],
  "columns": ["region", "owner", "amount", "salary"],
  "column_groups": {"salary": ["hr"]},
  "max_rows": 10000
}
```

Users sign in at `/sso`. The browser gets a session cookie, and API clients get a bearer token.
Services can also send an OIDC provider's own JWT.

```bash
curl -H "Authorization: Bearer $TOKEN" "https://dah.example.com/data/sales?f.region=eu&format=csv"
```

Row filters take their values from the caller's claims and fail closed when a claim is missing.
User filters are bound parameters. Every request is written to the access log. The "Simulate"
button shows the decision, the generated SQL and a preview for a hypothetical caller.

IdP setup values (shown on each provider's page):
- OIDC redirect URI: `<DAH_PUBLIC_BASE_URL>/sso/<id>/callback`
- SAML ACS: `<DAH_PUBLIC_BASE_URL>/sso/<id>/acs`; SP metadata: `/sso/<id>/metadata`

### 7. Accounts and sign-in

- **Local accounts**: an admin creates them under Users, or with `dah user create <name> [--admin]`.
  Passwords are hashed with scrypt. Failed logins are throttled (10 per 15 minutes per user and
  IP). Changing a password or disabling an account signs that user out everywhere.
- **OIDC, SAML 2.0 and LDAP / Active Directory**: add as many providers as you need under
  Access → Identity providers. OIDC and SAML appear as buttons on the sign-in screen, and LDAP
  adds an entry to its "Sign in with" list. The first sign-in creates an account, and IdP groups
  refresh at every sign-in. Members of a provider's `admin_groups` become site administrators.
- **Sessions**: the browser gets an HttpOnly cookie, and writes made with it need the
  `X-DAH-CSRF: 1` header (the UI sends it). The same session works for the UI, the API and `/data`.
- **Personal API tokens** (Account page): `Authorization: Bearer dah_pat_…` for scripts and MCP
  clients. A token acts with its owner's role and folder permissions.
- **Admin token**: `DAH_ADMIN_TOKEN` is a break-glass credential with full access.

Administrators manage pipelines, tables, settings, providers, endpoints and users. Everyone else
works with documents and SQL inside the folders they've been granted.

### 8. Per-folder permissions

Document paths form folders (`finance/2026/q1.pdf` is in `finance/2026`). Under Folders, admins
and folder managers grant a level to a **user** (username or email), a **group** (local or from
the IdP) or **all signed-in users**. A grant applies to the folder and everything below it:

| Level | Allows |
|---|---|
| read | list, open, download, search, version history, SQL over those documents |
| write | + upload new documents or versions, restore versions, reprocess |
| manage | + delete documents, grant and revoke permissions inside the folder |

Enforcement happens on the server everywhere documents are reachable: the documents API, search,
version history, the schema used for autofill, MCP (which acts as the calling user) and SQL. A
non-admin's SQL session contains only readable documents. DuckDB may open only those documents'
exact Parquet files, so `read_parquet()` or glob tricks can't bypass the filters. Pipeline output
tables aren't visible to non-admins, because they may combine data across folders; publish them
through `/data` endpoints instead. Data endpoints can also apply folder permissions with
`"folder_column": "path"` in their policy. Documents a user can't read return 404, the same as
documents that don't exist.

### 9. License acknowledgements

`/licenses` (public) lists every Python package in dah's dependency closure, plus the bundled
system and frontend components, with their licenses and full license texts. Tests fail if a
dependency isn't open source. To regenerate the file:
`dah licenses --markdown > THIRD_PARTY_LICENSES.md`.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `DAH_ADMIN_TOKEN` | `change-me` | admin API / UI / MCP token |
| `DAH_SECRET_KEY` | dev value | signs SSO sessions, encrypts stored secrets (keep it stable) |
| `DAH_PUBLIC_BASE_URL` | `http://localhost:8000` | external URL used for SSO redirects (use a real hostname for SAML) |
| `DAH_DATA_DIR` | `./data` (`/data` in Docker) | blobs, Parquet, SQLite catalog |
| `DAH_DATABASE_URL` | SQLite in data dir | external catalog |
| `DAH_MCP_TOKEN` | admin token | credential the MCP server uses on stdio (HTTP clients send their own) |
| `DAH_BOOTSTRAP_ADMIN_USER` / `_PASSWORD` | unset | local admin created at first start |
| `DAH_LOCAL_LOGIN_ENABLED` | `true` | allow local-account passwords (set `false` for SSO-only) |
| `DAH_PASSWORD_MIN_LENGTH` | `10` | local password policy |
| `DAH_OCR_LANGUAGES` | `eng` | Tesseract languages (install more `tesseract-ocr-*` packs in the image) |
| `DAH_OCR_MIN_PAGE_CHARS` | `25` | PDF pages with less text are OCR'd |
| `DAH_INGEST_WORKERS` | `2` | background workers |
| `DAH_MAX_UPLOAD_MB` | `512` | largest accepted upload |
| `DAH_CLAMAV_URL` | unset (off) | clamd for upload virus scanning: `tcp://host:3310` or `unix:///path` |
| `DAH_CLAMAV_TIMEOUT_S` | `60` | clamd connection / scan timeout |
| `DAH_CLAMAV_FAIL_OPEN` | `false` | accept uploads when clamd can't give a verdict |
| `DAH_PIPELINE_TIMEOUT_S` / `DAH_PIPELINE_MEMORY_MB` | `300` / `2048` | pipeline limits |
| `DAH_SESSION_TTL_S` | `28800` | SSO session lifetime |

## Security notes

- Admin endpoints need an admin account or `DAH_ADMIN_TOKEN`. Document endpoints need an account,
  and folder permissions apply. `/data/*` follows each endpoint's policy. `/licenses`, `/sso` and
  `/auth/methods` are public.
- Ad-hoc SQL runs in DuckDB with external file access restricted to the lake directory, and the
  configuration is locked.
- Pipeline code is **trusted admin code**. Its subprocess sandbox limits time and memory and
  hides `DAH_ADMIN_TOKEN`, `DAH_SECRET_KEY` and provider API keys, but it's not a security
  boundary against a malicious admin. In particular, it can read the catalog
  (`DAH_DATABASE_URL`).
- Stored secrets (ML API key, OIDC client secrets, SAML SP keys) are encrypted with a key derived
  from `DAH_SECRET_KEY`. If you change that variable, re-enter them.
- Uploads are only virus-scanned when `DAH_CLAMAV_URL` is set. Files stored before scanning was
  enabled are not rescanned.
- Run behind TLS in production. Session cookies are `Secure` when `DAH_PUBLIC_BASE_URL` is
  https.

## Project layout

```
dah/            config, db (catalog), storage (blobs), lake (DuckDB), jobs, schema, versioning,
                settings_store, assist (ML), licenses, mcp_server, cli
dah/ingest/     detect, extractors (OCR), service
dah/pipelines/  context (ctx/doc API), worker (sandbox), service
dah/auth/       identity (sessions, API tokens, provisioning), passwords, ldap, folders (permissions)
dah/sso/        core (principals, tokens), oidc, saml, acl (policies)
dah/api/        FastAPI routers
dah/web/static/ single-page UI (no build step)
tests/          pytest suite (+ tests/js for editor autocomplete)
agent-status/   per-feature task lists and progress logs
```
