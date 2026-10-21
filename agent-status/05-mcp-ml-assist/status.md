# Feature 05 — MCP server + configurable ML API key for editing pipeline queries

Status: NOT STARTED

## Tasks
- [ ] Encrypted settings store (Fernet, key derived from `DAH_SECRET_KEY`)
- [ ] ML settings API (provider, model, API key, base URL, effort) — key never returned in clear
- [ ] Assist endpoint: rewrite pipeline code from an instruction, schema-aware prompt
- [ ] Anthropic provider (official SDK, `claude-opus-5-5` default) + OpenAI-compatible provider for local models
- [ ] MCP server (stdio + streamable HTTP) with document/query/pipeline/version tools
- [ ] UI hook: "Ask AI" panel in the pipeline editor with diff preview
- [ ] Tests (provider mocked)

## Log
