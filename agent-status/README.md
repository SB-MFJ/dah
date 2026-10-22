# Agent status

One folder per feature; each `status.md` holds the task list and a dated log,
updated after each completed portion of work. Every finished feature is a
separate local git commit.

| # | Feature | Status file |
|---|---------|-------------|
| 01 | Core platform | [01-core-platform/status.md](01-core-platform/status.md) |
| 02 | Ingestion, metadata, OCR | [02-ingestion/status.md](02-ingestion/status.md) |
| 03 | Pipelines / transformations | [03-pipelines/status.md](03-pipelines/status.md) |
| 04 | Editor with autofill | [04-editor-autofill/status.md](04-editor-autofill/status.md) |
| 05 | MCP server + ML assist | [05-mcp-ml-assist/status.md](05-mcp-ml-assist/status.md) |
| 06 | Versioning | [06-versioning/status.md](06-versioning/status.md) |
| 07 | License acknowledgements | [07-licenses/status.md](07-licenses/status.md) |
| 08 | SSO + access-controlled data API | [08-sso-access-control/status.md](08-sso-access-control/status.md) |

Features 01–11: COMPLETE (2026-10-21). Test suite: 78 passed, 1 skipped (OCR test needs tesseract).
Commit order: 01 → 02 → 03 → 04 → 06 → 05 → 08 → 07 (versioning before MCP so MCP could expose restore;
licenses last so the inventory covers every dependency).

| 09 | Local auth, users, API tokens | [09-local-auth/status.md](09-local-auth/status.md) |
| 10 | Site login via OIDC / SAML / LDAP | [10-site-sso-ldap/status.md](10-site-sso-ldap/status.md) |
| 11 | Per-folder permissions | [11-folder-permissions/status.md](11-folder-permissions/status.md) |
| 12 | Upload virus scanning (ClamAV) | [12-virus-scanning/status.md](12-virus-scanning/status.md) |
