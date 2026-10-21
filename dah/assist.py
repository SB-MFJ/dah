"""ML assistant that edits pipeline code (Python or SQL) from a natural-language instruction.

Providers:
  anthropic          Claude via the official ``anthropic`` SDK (default model ``claude-opus-5-5``)
  openai_compatible  any /v1/chat/completions server, for self-hosted models (Ollama, vLLM, …)

The API key is configured at runtime (UI / API / MCP) and stored encrypted in the catalog.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import httpx

from . import schema as schema_mod
from . import settings_store

DEFAULTS = {
    "provider": "anthropic",
    "model": "claude-opus-5-5",
    "base_url": "",
    "effort": "medium",
    "max_tokens": "64000",
    "fallbacks": "true",
}
PROVIDERS = ("anthropic", "openai_compatible")
EFFORTS = ("low", "medium", "high", "xhigh", "max")


class AssistError(RuntimeError):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


@dataclass
class MLConfig:
    provider: str
    model: str
    base_url: str
    effort: str
    max_tokens: int
    fallbacks: bool
    api_key: str | None


def load_config() -> MLConfig:
    g = lambda k: settings_store.get(f"ml.{k}", DEFAULTS[k])  # noqa: E731
    return MLConfig(
        provider=g("provider"),
        model=g("model"),
        base_url=g("base_url") or "",
        effort=g("effort"),
        max_tokens=int(g("max_tokens")),
        fallbacks=g("fallbacks") == "true",
        api_key=settings_store.get("ml.api_key"),
    )


def save_config(changes: dict) -> MLConfig:
    for k in ("provider", "model", "base_url", "effort", "max_tokens", "fallbacks"):
        if k in changes and changes[k] is not None:
            v = changes[k]
            if k == "provider" and v not in PROVIDERS:
                raise ValueError(f"provider must be one of {PROVIDERS}")
            if k == "effort" and v not in EFFORTS:
                raise ValueError(f"effort must be one of {EFFORTS}")
            if k == "fallbacks":
                v = "true" if v in (True, "true", "1", 1) else "false"
            settings_store.put(f"ml.{k}", str(v))
    if "api_key" in changes and changes["api_key"] is not None:
        # "" clears the key; omitted / None leaves it unchanged
        settings_store.put("ml.api_key", changes["api_key"] or None, secret=True)
    return load_config()


def public_config() -> dict:
    c = load_config()
    return {
        "provider": c.provider,
        "model": c.model,
        "base_url": c.base_url,
        "effort": c.effort,
        "max_tokens": c.max_tokens,
        "fallbacks": c.fallbacks,
        "api_key_set": bool(c.api_key),
        "api_key_hint": settings_store.hint(c.api_key),
    }


# --------------------------------------------------------------------------- prompt

SYSTEM = """You edit data-pipeline code for "dah", a self-hosted document lakehouse built on DuckDB.

Pipeline kinds:
- python: a manual pipeline defines `def run(ctx)` and returns rows (list of dicts, pandas DataFrame or
  pyarrow Table) or calls `ctx.write_table(name, rows, mode="replace"|"append")`. An on_ingest pipeline
  defines `def transform(doc, ctx)` and returns a dict that is merged into `doc.metadata`.
- sql: a single DuckDB SELECT whose result becomes the pipeline's output table.

DuckDB notes: metadata columns are JSON. Use `metadata->>'key'` for text and `metadata->'a'->>'b'` for
nested keys, CAST for numbers, and wrap comparisons in parentheses because `->>` binds looser than `=`:
`(metadata->>'author') = 'Ada'`.

Only use tables, columns, metadata keys and ctx/doc members that appear in the schema below, unless the
user asks you to create something new. Keep the user's existing structure and style where possible.

Reply with the complete revised code in exactly one fenced code block, then at most five short bullet
points describing what changed. No other prose."""


def schema_context(max_keys: int = 150) -> str:
    s = schema_mod.schema()
    lines = ["# Tables and views"]
    for name, cols in s["views"].items():
        lines.append(f"- {name}(" + ", ".join(f"{c['name']} {c['type']}" for c in cols) + ")")
    lines.append("\n# Document metadata keys (path: types, number of documents, sample)")
    for k in s["metadata_keys"][:max_keys]:
        lines.append(f"- {k['path']}: {'|'.join(k['types'])}, {k['count']}" + (f", e.g. {k['sample']}" if k.get("sample") else ""))
    if s["record_keys"]:
        lines.append("\n# Keys of records.data (rows extracted from spreadsheets / CSV / tables)")
        lines.extend(f"- {k['path']}" for k in s["record_keys"][:max_keys])
    lines.append("\n# ctx API")
    lines.extend(f"- ctx.{m['signature']}: {m['doc'].splitlines()[0] if m['doc'] else ''}" for m in s["api"]["ctx"])
    lines.append("\n# doc API")
    lines.extend(f"- doc.{m['signature']}: {m['doc'].splitlines()[0] if m['doc'] else ''}" for m in s["api"]["doc"])
    return "\n".join(lines)


def build_user_message(kind: str, code: str, instruction: str, pipeline: str | None) -> str:
    return (
        f"Pipeline: {pipeline or '(new)'}\nKind: {kind}\n\n"
        f"<current_code>\n{code}\n</current_code>\n\n"
        f"<instruction>\n{instruction}\n</instruction>"
    )


FENCE = re.compile(r"```[a-zA-Z0-9_+-]*\n(.*?)```", re.S)


def parse_reply(text: str) -> tuple[str, str]:
    m = FENCE.search(text)
    if not m:
        raise AssistError("model reply contained no code block")
    code = m.group(1).rstrip() + "\n"
    explanation = (text[: m.start()] + text[m.end():]).strip()
    return code, explanation


# --------------------------------------------------------------------------- providers


def _call_anthropic(cfg: MLConfig, system: str, user: str) -> tuple[str, dict]:
    import anthropic

    client = anthropic.Anthropic(api_key=cfg.api_key, base_url=cfg.base_url or None, max_retries=2)
    kwargs: dict = {}
    if cfg.fallbacks and not cfg.base_url:
        # Server-side fallback: if the model declines, Anthropic retries on an appropriate model.
        kwargs["extra_headers"] = {"anthropic-beta": "server-side-fallback-2026-07-01"}
        kwargs["extra_body"] = {"fallbacks": "default"}
    try:
        with client.messages.stream(
            model=cfg.model,
            max_tokens=cfg.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            thinking={"type": "adaptive"},
            output_config={"effort": cfg.effort},
            **kwargs,
        ) as stream:
            msg = stream.get_final_message()
    except anthropic.AuthenticationError as e:
        raise AssistError("the configured API key was rejected", 400) from e
    except anthropic.PermissionDeniedError as e:
        raise AssistError(f"permission denied: {e.message}", 403) from e
    except anthropic.NotFoundError as e:
        raise AssistError(f"model not found: {cfg.model}", 400) from e
    except anthropic.RateLimitError as e:
        raise AssistError("rate limited by the provider; retry shortly", 429) from e
    except anthropic.BadRequestError as e:
        raise AssistError(f"provider rejected the request: {e.message}", 400) from e
    except anthropic.APIStatusError as e:
        raise AssistError(f"provider error {e.status_code}: {e.message}", 502) from e
    except anthropic.APIConnectionError as e:
        raise AssistError(f"cannot reach the provider: {e}", 502) from e

    if msg.stop_reason == "refusal":
        details = getattr(msg, "stop_details", None)
        raise AssistError(f"the model declined this request ({getattr(details, 'category', None) or 'no category'})", 422)
    if msg.stop_reason == "max_tokens":
        raise AssistError("reply was cut off at max_tokens; raise the limit in ML settings", 502)
    text = "".join(b.text for b in msg.content if b.type == "text")
    usage = {"input_tokens": msg.usage.input_tokens, "output_tokens": msg.usage.output_tokens, "model": msg.model}
    return text, usage


def _call_openai_compatible(cfg: MLConfig, system: str, user: str) -> tuple[str, dict]:
    if not cfg.base_url:
        raise AssistError("openai_compatible provider needs a base_url (e.g. http://ollama:11434/v1)", 400)
    headers = {"Authorization": f"Bearer {cfg.api_key}"} if cfg.api_key else {}
    try:
        r = httpx.post(
            cfg.base_url.rstrip("/") + "/chat/completions",
            json={"model": cfg.model, "max_tokens": cfg.max_tokens, "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
            headers=headers,
            timeout=600,
        )
    except httpx.HTTPError as e:
        raise AssistError(f"cannot reach the provider: {e}") from e
    if r.status_code >= 400:
        raise AssistError(f"provider error {r.status_code}: {r.text[:300]}", 400 if r.status_code in (400, 401, 403, 404) else 502)
    data = r.json()
    text = data["choices"][0]["message"]["content"] or ""
    return text, {"model": data.get("model", cfg.model), **(data.get("usage") or {})}


def complete(system: str, user: str, cfg: MLConfig | None = None) -> tuple[str, dict]:
    cfg = cfg or load_config()
    if cfg.provider == "anthropic" and not cfg.api_key:
        raise AssistError("no ML API key configured (Settings → ML assistant)", 400)
    if cfg.provider == "anthropic":
        return _call_anthropic(cfg, system, user)
    return _call_openai_compatible(cfg, system, user)


def edit_pipeline(kind: str, code: str, instruction: str, pipeline: str | None = None) -> dict:
    if kind not in ("python", "sql"):
        raise AssistError("kind must be python or sql", 400)
    if not instruction.strip():
        raise AssistError("instruction is empty", 400)
    system = SYSTEM + "\n\n" + schema_context()
    text, usage = complete(system, build_user_message(kind, code, instruction, pipeline))
    new_code, explanation = parse_reply(text)
    from .pipelines.service import validate

    return {"code": new_code, "explanation": explanation, "problems": validate(kind, new_code), "usage": usage}
