"""LLM provider adapter for the Ask assistant (Phase 3b).

Interface: ``provider.chat(messages, tools) -> {"content", "tool_calls", "model"}``
where ``tool_calls`` is a list of ``{"id", "name", "arguments": dict}`` (arguments
already parsed; ``None`` if the model produced unparseable JSON). ``tools`` is a
list of ``{name, description, parameters}`` schemas; pass ``[]`` to forbid tool
use. Only DeepSeek (OpenAI-compatible) is implemented, via stdlib urllib.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Protocol

import ai_verify

_ENDPOINT = "https://api.deepseek.com/chat/completions"
_MODEL = "deepseek-chat"
_TIMEOUT = 30.0


class ProviderError(RuntimeError):
    """The model provider failed (network, HTTP, or malformed response)."""


class Provider(Protocol):
    model: str

    def chat(self, messages: list[dict], tools: list[dict]) -> dict: ...


class DeepSeekProvider:
    model = _MODEL

    def __init__(self, api_key: str, timeout: float = _TIMEOUT):
        self._key = api_key
        self._timeout = timeout

    def chat(self, messages: list[dict], tools: list[dict]) -> dict:
        body: dict[str, Any] = {"model": self.model, "temperature": 0, "messages": messages}
        if tools:
            body["tools"] = [{"type": "function", "function": t} for t in tools]
            body["tool_choice"] = "auto"
        req = urllib.request.Request(
            _ENDPOINT, data=json.dumps(body).encode("utf-8"),
            headers={"Authorization": f"Bearer {self._key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            msg = payload["choices"][0]["message"]
        except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, IndexError) as exc:
            # Only the exception type is surfaced: its text may echo request details.
            raise ProviderError(f"provider request failed ({type(exc).__name__})") from exc
        calls = []
        for tc in msg.get("tool_calls") or []:
            fn = (tc or {}).get("function") or {}
            raw = fn.get("arguments")
            try:
                if isinstance(raw, str):
                    args = json.loads(raw) if raw.strip() else {}
                else:
                    args = raw if raw is not None else {}
            except ValueError:
                args = None
            calls.append({"id": tc.get("id") or f"call_{len(calls)}", "name": fn.get("name"), "arguments": args})
        return {"content": msg.get("content") or "", "tool_calls": calls, "model": self.model}


def get_provider() -> Provider | None:
    """Provider selected by env ``ASK_PROVIDER`` (default deepseek); None when
    unknown or unconfigured (the route answers 503)."""
    name = (os.environ.get("ASK_PROVIDER") or "deepseek").strip().lower()
    if name == "deepseek":
        key = ai_verify.resolve_api_key()
        return DeepSeekProvider(key) if key else None
    return None
