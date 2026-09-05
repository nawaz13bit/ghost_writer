"""Editable agent system-prompt endpoints, backed by prompt_overrides.json."""
from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ghostwriter.webui import state

router = APIRouter(prefix="/api/prompts", tags=["prompts"])


@router.get("")
def list_prompts() -> list[dict[str, Any]]:
    return [
        {
            "key": key,
            "label": label,
            "default": state.DEFAULT_PROMPTS[key],
            "current": agent.system_prompt,
            "overridden": key in state.prompt_overrides,
        }
        for key, (label, agent) in state.AGENT_REGISTRY.items()
    ]


class PromptUpdateRequest(BaseModel):
    # None or empty resets the agent to its built-in default prompt.
    system_prompt: str | None = None


@router.put("/{key}")
def update_prompt(key: str, req: PromptUpdateRequest) -> dict[str, Any]:
    if key not in state.AGENT_REGISTRY:
        raise HTTPException(404, f"Unknown agent {key!r}")
    text = req.system_prompt.strip() if req.system_prompt and req.system_prompt.strip() else None
    if text is None or text == state.DEFAULT_PROMPTS[key]:
        state.prompt_overrides.pop(key, None)
    else:
        state.prompt_overrides[key] = text
    state.PROMPT_OVERRIDES_PATH.write_text(
        json.dumps(state.prompt_overrides, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    label, agent = state.AGENT_REGISTRY[key]
    agent.system_prompt = state.prompt_overrides.get(key, state.DEFAULT_PROMPTS[key])
    return {
        "key": key,
        "label": label,
        "default": state.DEFAULT_PROMPTS[key],
        "current": agent.system_prompt,
        "overridden": key in state.prompt_overrides,
    }
