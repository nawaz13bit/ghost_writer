"""Base class shared by every book-writing agent."""
from __future__ import annotations

import json
import re
from typing import Callable

from ghostwriter.llm_client import LLMClient


class AIOutputError(ValueError):
    """Raised when the model's reply doesn't have the shape a caller needs
    (wrong JSON type, missing keys, empty list) - distinct from a plain
    ValueError so callers can tell "the AI messed up" apart from their own
    validation errors (e.g. "no such chapter") and map it to its own
    user-facing response."""


class Agent:
    #: Overridden by subclasses; describes the agent's role/voice to the model.
    system_prompt: str = "You are a helpful assistant."

    def __init__(self, llm: LLMClient):
        self.llm = llm

    def ask(self, user_prompt: str, **gen_kwargs) -> str:
        return self.llm.chat(self.system_prompt, user_prompt, **gen_kwargs)

    def ask_stream(self, user_prompt: str, on_delta: Callable[[str], None], **gen_kwargs) -> str:
        return self.llm.chat_stream(self.system_prompt, user_prompt, on_delta, **gen_kwargs)

    def ask_json(self, user_prompt: str, retries: int = 2, **gen_kwargs) -> dict | list:
        """Calls the model expecting a JSON payload and parses it, retrying on failure."""
        prompt = user_prompt
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            raw = self.ask(prompt, **gen_kwargs)
            try:
                return extract_json(raw)
            except (ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                prompt = (
                    f"{user_prompt}\n\n"
                    f"Your previous reply could not be parsed as JSON ({exc}). "
                    "Reply with ONLY valid JSON, no prose, no markdown fences."
                )
        raise AIOutputError(f"Could not get valid JSON after {retries + 1} attempts: {last_error}")

    def ask_json_list(self, user_prompt: str, required_key: str = "chapter_num", **kwargs) -> list[dict]:
        """Like ask_json, but also validates the model actually returned a
        non-empty list of objects each carrying required_key - callers that
        immediately index/sort on that key would otherwise crash with a raw
        IndexError/KeyError on a malformed reply instead of a clear error."""
        result = self.ask_json(user_prompt, **kwargs)
        if isinstance(result, dict):
            result = [result]
        if not isinstance(result, list) or not result:
            raise AIOutputError(f"Expected a non-empty JSON list, got: {result!r}"[:300])
        for entry in result:
            if not isinstance(entry, dict) or required_key not in entry:
                raise AIOutputError(
                    f"Expected each item to be an object with {required_key!r}, got: {entry!r}"[:300]
                )
        return result

    def ask_json_object(self, user_prompt: str, **kwargs) -> dict:
        """Like ask_json, but unwraps a single-item list (the model sometimes
        wraps a lone object in an array) and validates the result is a
        non-empty object, instead of letting callers crash on result[0] of
        an empty list."""
        result = self.ask_json(user_prompt, **kwargs)
        if isinstance(result, list):
            if not result:
                raise AIOutputError("Expected a JSON object, got an empty list")
            result = result[0]
        if not isinstance(result, dict):
            raise AIOutputError(f"Expected a JSON object, got: {result!r}"[:300])
        return result


_DELIMITER_LINE = re.compile(r"^---[^\n]*---\s*$")


def strip_echoed_delimiters(text: str) -> str:
    """Strips leading/trailing '--- LABEL ---' marker lines the model
    sometimes echoes back verbatim from the prompt (e.g. '--- CHAPTER 1 ---'
    / '--- END CHAPTER ---') instead of returning bare prose, even when told
    to output only the text - these aren't part of the chapter and must not
    get saved as if they were."""
    lines = text.strip().split("\n")
    while lines and _DELIMITER_LINE.match(lines[0].strip()):
        lines.pop(0)
    while lines and _DELIMITER_LINE.match(lines[-1].strip()):
        lines.pop()
    return "\n".join(lines).strip()


def extract_json(text: str) -> dict | list:
    """Pulls a JSON object/array out of a model reply that may include prose or fences."""
    text = text.strip()
    fence_match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    start_chars = "{["
    for i, ch in enumerate(text):
        if ch in start_chars:
            close = "}" if ch == "{" else "]"
            depth = 0
            for j in range(i, len(text)):
                if text[j] == ch:
                    depth += 1
                elif text[j] == close:
                    depth -= 1
                    if depth == 0:
                        candidate = text[i:j + 1]
                        return json.loads(candidate)
            break
    raise ValueError(f"No JSON object/array found in text: {text[:200]!r}")
