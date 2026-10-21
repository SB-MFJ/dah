from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from .test_ingest import upload


# --------------------------------------------------------------------------- settings


def test_ml_settings_key_is_encrypted_and_hidden(client, env):
    s = client.get("/api/settings/ml").json()
    assert s["provider"] == "anthropic" and s["model"] == "claude-opus-5-5" and s["api_key_set"] is False
    r = client.put("/api/settings/ml", json={"api_key": "sk-ant-secret-1234", "effort": "high"}).json()
    assert r["api_key_set"] is True and r["api_key_hint"] == "…1234" and r["effort"] == "high"
    assert "sk-ant-secret-1234" not in json.dumps(r)
    # stored encrypted
    from dah.db import Setting, session_scope

    with session_scope() as db:
        row = db.get(Setting, "ml.api_key")
        assert row.encrypted and "secret" not in row.value
    # omitted key = unchanged, "" = cleared
    assert client.put("/api/settings/ml", json={"model": "claude-sonnet-5-5"}).json()["api_key_set"] is True
    assert client.put("/api/settings/ml", json={"api_key": ""}).json()["api_key_set"] is False
    assert client.put("/api/settings/ml", json={"effort": "extreme"}).status_code == 422


# --------------------------------------------------------------------------- assist (provider mocked)


class FakeStream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.msg


def fake_anthropic(monkeypatch, text="```sql\nSELECT path FROM documents\n```\n- selects paths", stop_reason="end_turn"):
    calls = []

    class FakeClient:
        def __init__(self, **kw):
            calls.append(("init", kw))
            self.messages = SimpleNamespace(stream=self._stream)

        def _stream(self, **kw):
            calls.append(("stream", kw))
            msg = SimpleNamespace(
                stop_reason=stop_reason, stop_details=SimpleNamespace(category="cyber"), model="claude-opus-5-5",
                content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
                usage=SimpleNamespace(input_tokens=10, output_tokens=5),
            )
            return FakeStream(msg)

    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", FakeClient)
    return calls


def test_assist_requires_key(client):
    r = client.post("/api/assist/pipeline", json={"kind": "sql", "code": "SELECT 1", "instruction": "x"})
    assert r.status_code == 400 and "API key" in r.json()["detail"]


def test_assist_anthropic_request_shape(client, monkeypatch):
    upload(client, "a.txt", b"hello", metadata={"owner": "ops"})
    client.put("/api/settings/ml", json={"api_key": "sk-test-abcdef"})
    calls = fake_anthropic(monkeypatch)
    r = client.post("/api/assist/pipeline", json={"kind": "sql", "code": "SELECT 1", "instruction": "list paths", "pipeline": "p"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["code"] == "SELECT path FROM documents\n" and "selects paths" in body["explanation"] and body["problems"] == []
    init = dict(calls)["init"]
    kw = dict(calls)["stream"]
    assert init["api_key"] == "sk-test-abcdef"
    assert kw["model"] == "claude-opus-5-5" and kw["thinking"] == {"type": "adaptive"} and kw["output_config"] == {"effort": "medium"}
    assert kw["extra_body"] == {"fallbacks": "default"}
    assert "documents(" in kw["system"] and "user.owner" in kw["system"] and "ctx.write_table" in kw["system"]
    assert "<instruction>\nlist paths\n</instruction>" in kw["messages"][0]["content"]


def test_assist_refusal_and_bad_reply(client, monkeypatch):
    client.put("/api/settings/ml", json={"api_key": "sk-test-abcdef"})
    fake_anthropic(monkeypatch, stop_reason="refusal")
    r = client.post("/api/assist/pipeline", json={"kind": "sql", "code": "SELECT 1", "instruction": "x"})
    assert r.status_code == 422 and "declined" in r.json()["detail"]
    fake_anthropic(monkeypatch, text="no code here")
    r = client.post("/api/assist/pipeline", json={"kind": "sql", "code": "SELECT 1", "instruction": "x"})
    assert r.status_code == 502 and "no code block" in r.json()["detail"]


def test_assist_openai_compatible(client, monkeypatch):
    client.put("/api/settings/ml", json={"provider": "openai_compatible", "base_url": "http://llm:11434/v1", "model": "qwen-coder"})
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        seen.update(url=url, body=json, headers=headers)
        return SimpleNamespace(status_code=200, json=lambda: {"model": "qwen-coder", "choices": [{"message": {"content": "```python\ndef run(ctx):\n    return []\n```"}}]}, text="")

    monkeypatch.setattr("dah.assist.httpx.post", fake_post)
    r = client.post("/api/assist/pipeline", json={"kind": "python", "code": "", "instruction": "empty"})
    assert r.status_code == 200, r.text
    assert seen["url"] == "http://llm:11434/v1/chat/completions" and seen["body"]["model"] == "qwen-coder" and seen["headers"] == {}
    assert r.json()["code"].startswith("def run(ctx):")


# --------------------------------------------------------------------------- MCP


def call(tool, args=None):
    from dah import mcp_server

    res = asyncio.run(mcp_server.mcp.call_tool(tool, args or {}))
    assert not res.is_error, res.content[0].text
    if res.structured_content and "result" in res.structured_content:
        return res.structured_content["result"]
    return json.loads(res.content[0].text) if res.content else None


def test_mcp_tools_end_to_end(client, monkeypatch):
    from dah import mcp_server

    mcp_server.set_client(client)
    upload(client, "memo.txt", b"budget approved for 2027", path="memos/memo.txt")

    names = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert {"list_documents", "get_document", "search_text", "run_sql", "describe_schema", "create_pipeline", "update_pipeline",
            "run_pipeline", "assist_edit_pipeline", "list_versions", "restore_version", "diff_versions"} <= names

    docs = call("list_documents", {"query": "memo"})
    assert docs["total"] == 1 and docs["items"][0]["path"] == "memos/memo.txt"
    doc_id = docs["items"][0]["id"]
    assert "budget" in call("get_document", {"document_id": doc_id, "include_text": True})["text"]
    assert call("search_text", {"query": "BUDGET"})[0]["page"] == 1
    assert call("run_sql", {"sql": "SELECT count(*) n FROM documents"})["rows"] == [{"n": 1}]
    assert "documents" in call("describe_schema")["views"]

    call("create_pipeline", {"name": "cnt", "kind": "sql", "code": "SELECT count(*) AS n FROM documents", "output_table": "cnt"})
    assert call("run_pipeline", {"name": "cnt"})["status"] == "succeeded"
    call("update_pipeline", {"name": "cnt", "code": "SELECT 42 AS n", "comment": "fixed"})
    assert [v["version"] for v in call("list_versions", {"kind": "pipeline", "ref": "cnt"})["items"]] == [2, 1]
    assert "+SELECT 42 AS n" in call("diff_versions", {"kind": "pipeline", "ref": "cnt", "a": 1, "b": 2})["code_diff"]
    assert call("restore_version", {"kind": "pipeline", "ref": "cnt", "version": 1})["new_version"] == 3

    # assist via MCP with mocked provider, applied as a new version
    client.put("/api/settings/ml", json={"api_key": "sk-test-abcdef"})
    fake_anthropic(monkeypatch, text="```sql\nSELECT 7 AS n\n```")
    r = call("assist_edit_pipeline", {"name": "cnt", "instruction": "return 7", "apply": True})
    assert r["applied"] is True and r["new_version"] == 4
    assert call("get_pipeline", {"name": "cnt"})["current"]["comment"] == "AI: return 7"


def test_mcp_http_requires_bearer(env, monkeypatch):
    from fastapi.testclient import TestClient

    from dah import mcp_server

    monkeypatch.setenv("DAH_MCP_TOKEN", "mcp-secret")
    with TestClient(mcp_server.http_app()) as c:
        assert c.post("/mcp", json={}).status_code == 401
        assert c.post("/mcp", json={}, headers={"Authorization": "Bearer wrong"}).status_code == 401
        r = c.post(
            "/mcp",
            headers={"Authorization": "Bearer mcp-secret", "Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                  "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}},
        )
        assert r.status_code == 200, r.text
