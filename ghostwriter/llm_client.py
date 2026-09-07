"""Thin client for llama-server's OpenAI-compatible /v1/chat/completions endpoint."""
from __future__ import annotations

import itertools
import json
import threading
import time
from typing import Callable

import requests


class LLMCancelled(RuntimeError):
    """Raised when an in-flight LLM call is stopped via the emergency-stop control."""


class LLMTruncated(RuntimeError):
    """Raised when the server stops generating because max_tokens ran out
    (finish_reason "length") rather than reaching a natural end. The partial
    text is still attached so a caller could recover it, but callers should
    generally treat this as a failed generation rather than a finished one -
    silently saving cut-off prose as a completed draft/scene would be worse
    than surfacing the error."""

    def __init__(self, partial_text: str, max_tokens: int):
        self.partial_text = partial_text
        self.max_tokens = max_tokens
        super().__init__(
            f"The model ran out of tokens (limit was {max_tokens}) before finishing - "
            "the output was cut off mid-generation. Try again with a shorter target "
            "length, or increase generation.max_tokens / raise chapter_target_words "
            "headroom in config."
        )


# Registry of in-flight calls' cancel signals, so a single "stop" action from
# the UI can halt whatever's running right now regardless of which agent or
# job thread started it - this is a single-user local app, so one global
# "stop everything in flight" is simpler and more useful than per-call
# cancel handles the UI would have to track and target individually.
_registry_lock = threading.Lock()
_active_events: dict[int, threading.Event] = {}
_call_ids = itertools.count()


def cancel_all_calls() -> int:
    """Signals every in-flight LLM call to stop ASAP. Returns how many were live."""
    with _registry_lock:
        events = list(_active_events.values())
    for event in events:
        event.set()
    return len(events)


class LLMClient:
    def __init__(self, cfg: dict):
        llama = cfg["llama"]
        gen = cfg.get("generation", {})
        self.base_url = f"http://{llama['host']}:{llama['port']}"
        self.default_temperature = gen.get("temperature", 0.8)
        self.default_top_p = gen.get("top_p", 0.95)
        self.default_max_tokens = gen.get("max_tokens", 2048)

    def is_up(self, timeout: float = 2.0) -> bool:
        try:
            r = requests.get(f"{self.base_url}/health", timeout=timeout)
            return r.status_code == 200
        except requests.RequestException:
            return False

    def wait_until_up(self, timeout_s: float = 300.0, poll_s: float = 2.0) -> None:
        start = time.time()
        while time.time() - start < timeout_s:
            if self.is_up():
                return
            time.sleep(poll_s)
        raise TimeoutError(
            f"llama-server not reachable at {self.base_url} after {timeout_s}s. "
            "Start it with: powershell -File scripts/start_llm_server.ps1"
        )

    def _stream_request(self, payload: dict, on_delta: Callable[[str], None] | None) -> str:
        """Posts to /v1/chat/completions with stream:true and accumulates the
        SSE deltas, checking a per-call cancel event between chunks so an
        emergency-stop from the UI can abort mid-generation - closing the
        response here drops the connection, which llama.cpp's server detects
        as a client disconnect and stops generating rather than burning
        through the rest of max_tokens for an answer nobody will see.
        Shared by chat() and chat_stream(); on_delta is None for chat(),
        which just wants the final accumulated text."""
        event = threading.Event()
        call_id = next(_call_ids)
        with _registry_lock:
            _active_events[call_id] = event
        try:
            accumulated = []
            finish_reason = None
            with requests.post(f"{self.base_url}/v1/chat/completions", json=payload, timeout=600, stream=True) as r:
                r.raise_for_status()
                # The stream endpoint doesn't send a charset, so requests would
                # otherwise guess Latin-1 and mangle multi-byte UTF-8 chars
                # (e.g. curly apostrophes) into mojibake like "stationâ€™s".
                r.encoding = "utf-8"
                for line in r.iter_lines(decode_unicode=True):
                    if event.is_set():
                        r.close()
                        raise LLMCancelled("Stopped by user.")
                    if not line or not line.startswith("data: "):
                        continue
                    chunk = line[len("data: "):]
                    if chunk.strip() == "[DONE]":
                        break
                    try:
                        choice = json.loads(chunk)["choices"][0]
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
                    # finish_reason arrives on the last chunk (often alongside
                    # an empty delta), so it must be captured independently of
                    # whether this chunk carries any text.
                    finish_reason = choice.get("finish_reason") or finish_reason
                    delta = choice.get("delta", {}).get("content")
                    if delta:
                        accumulated.append(delta)
                        if on_delta is not None:
                            on_delta("".join(accumulated))
            text = "".join(accumulated).strip()
            if finish_reason == "length":
                raise LLMTruncated(text, payload["max_tokens"])
            return text
        finally:
            with _registry_lock:
                _active_events.pop(call_id, None)

    def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        enable_thinking: bool = False,
    ) -> str:
        # The local reasoning model burns thousands of hidden reasoning_content
        # tokens even on trivial prompts (observed: ~7000 chars of chain-of-thought
        # for a 2-sentence toy edit), and if max_tokens runs out before it finishes
        # deliberating, message.content comes back completely empty ("length" finish
        # reason) - the model never got to write the actual answer. None of our
        # editing/drafting tasks need visible chain-of-thought, so it's off by default.
        payload = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature if temperature is not None else self.default_temperature,
            "top_p": top_p if top_p is not None else self.default_top_p,
            "max_tokens": max_tokens if max_tokens is not None else self.default_max_tokens,
            "chat_template_kwargs": {"enable_thinking": enable_thinking},
            "stream": True,
        }
        # One retry on a transient connection hiccup (not a timeout - a call that
        # genuinely took 600s and got cut off should surface, not silently double
        # the wait) so a single dropped connection mid-draft doesn't kill the whole
        # chapter generation. A user-requested cancel (LLMCancelled) is not a
        # transient hiccup, so it's not caught here and surfaces immediately.
        last_exc: requests.RequestException | None = None
        for attempt in range(2):
            try:
                return self._stream_request(payload, on_delta=None)
            except requests.ConnectionError as exc:
                last_exc = exc
                if attempt == 0:
                    time.sleep(2.0)
        raise last_exc

    def chat_stream(
        self,
        system_prompt: str,
        user_prompt: str,
        on_delta: Callable[[str], None],
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
        enable_thinking: bool = False,
    ) -> str:
        """Like chat(), but calls on_delta(accumulated_text) as each SSE chunk
        arrives (accumulated, not just the delta, so callers can just assign
        it straight into a job's "partial_text" without tracking state
        themselves) and returns the final full text once the stream ends.
        Used for the two longest single calls (chapter draft/revise) so the
        UI can show live progress instead of a static busy label for
        30-90+ seconds. No retry here (unlike chat()) - resuming a partial
        SSE stream isn't meaningful, so a connection drop just surfaces."""
        payload = {
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": temperature if temperature is not None else self.default_temperature,
            "top_p": top_p if top_p is not None else self.default_top_p,
            "max_tokens": max_tokens if max_tokens is not None else self.default_max_tokens,
            "chat_template_kwargs": {"enable_thinking": enable_thinking},
            "stream": True,
        }
        return self._stream_request(payload, on_delta)
