# Feature 05 — MCP server + configurable ML API key for editing pipeline queries

Status: COMPLETE

## Tasks
- [x] Encrypted settings store (Fernet, key derived from `DAH_SECRET_KEY`)
- [x] ML settings API (provider, model, API key, base URL, effort) — key never returned in clear
- [x] Assist endpoint: rewrite pipeline code from an instruction, schema-aware prompt
- [x] Anthropic provider (official SDK, `claude-opus-5-5` default) + OpenAI-compatible provider for local models
- [x] MCP server (stdio + streamable HTTP) with document/query/pipeline/version tools
- [x] UI hook: "Ask AI" panel in the pipeline editor with diff preview
- [x] Tests (provider mocked)

## Log
- 2026-10-21: `settings_store.py` — catalog-backed settings; secrets encrypted with Fernet using an
  HKDF-SHA256 key derived from `DAH_SECRET_KEY`. Pipeline workers never receive the secret key,
  so user code can't decrypt stored keys.
- 2026-10-21: `assist.py` — the system prompt includes the live schema (views/columns, metadata keys
  with samples, record keys, ctx/doc API). Anthropic provider uses `anthropic` 2.x
  `messages.stream(...).get_final_message()` with `claude-opus-5-5`, adaptive thinking,
  configurable effort (default medium), and Anthropic's server-side fallback beta
  (`fallbacks: "default"`, toggle in settings). Handles `refusal` / `max_tokens` stop reasons and
  maps SDK errors (auth / not-found / rate-limit / connection) to HTTP codes. The
  OpenAI-compatible provider covers self-hosted models (Ollama, vLLM). Every proposal is
  validated (python AST / DuckDB EXPLAIN) before it's returned.
- 2026-10-21: API: `GET/PUT /api/settings/ml` (key write-only: omitted = keep, "" = clear; only
  `api_key_set` + last-4 hint are returned), `POST /api/assist/pipeline` (proposal only, never saves).
- 2026-10-21: `mcp_server.py` (MCP Python SDK 2.x `MCPServer`): 16 tools — list_documents,
  get_document, search_text, run_sql, describe_schema, list_tables, list_pipelines, get_pipeline,
  validate_pipeline, create_pipeline, update_pipeline, run_pipeline, assist_edit_pipeline (optional
  apply → new version with "AI:" comment), list_versions, diff_versions, restore_version. Read-only
  annotations on read tools. A thin REST client, so it runs as its own compose service.
  Streamable HTTP is wrapped in bearer-token middleware (`DAH_MCP_TOKEN`, falls back to the admin
  token).
- 2026-10-21: UI: Settings page (provider/model/base URL/effort/max tokens/fallback/key) and an
  "Ask AI" panel on the pipeline editor that shows a diff and validation status and applies to the
  editor only.
- 2026-10-21: 7 tests pass (provider mocked at the SDK boundary; MCP tools end-to-end against the
  app; HTTP transport rejects missing or wrong bearer tokens). The stdio transport was checked
  manually (initialize + tools/list → 16 tools). No live call to the Anthropic API was made
  (no key on this machine).
