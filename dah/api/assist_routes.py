"""ML assistant settings + pipeline editing endpoint."""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .. import assist
from .deps import Admin

router = APIRouter(prefix="/api", tags=["ml-assist"], dependencies=[Admin])


class MLSettingsIn(BaseModel):
    provider: Literal["anthropic", "openai_compatible"] | None = None
    model: str | None = None
    base_url: str | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    max_tokens: int | None = None
    fallbacks: bool | None = None
    api_key: str | None = None  # omitted = unchanged, "" = clear


class AssistIn(BaseModel):
    kind: Literal["python", "sql"]
    code: str = ""
    instruction: str
    pipeline: str | None = None


@router.get("/settings/ml")
def get_ml_settings():
    """ML settings. The API key is never returned, only whether it is set and its last 4 characters."""
    return assist.public_config()


@router.put("/settings/ml")
def put_ml_settings(body: MLSettingsIn):
    assist.save_config(body.model_dump(exclude_unset=True))
    return assist.public_config()


@router.post("/assist/pipeline")
def assist_pipeline(body: AssistIn):
    """Propose a revised pipeline (not saved). Returns code, explanation and validation problems."""
    try:
        return assist.edit_pipeline(body.kind, body.code, body.instruction, body.pipeline)
    except assist.AssistError as e:
        raise HTTPException(e.status, str(e))
