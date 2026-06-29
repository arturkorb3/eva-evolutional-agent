#!/usr/bin/env python3
"""Model adapters: the ONLY place that knows about a provider's wire format.

EVA-Core speaks `AgentTurn -> ModelResult`. An adapter translates that contract
to a concrete backend:

  - FakeAdapter            deterministic, offline; for tests and dry runs.
  - OpenAIChatAdapter      OpenAI-compatible Chat Completions (OpenAI, Azure,
                           Ollama, LM Studio, vLLM, OpenRouter, ...). Tools are
                           rendered as a portable JSON-text protocol so it works
                           even against backends without native function calling.

A future OpenAIResponsesAdapter (native tools + previous_response_id) slots in
here without touching the core.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

from core import AgentTurn, Event, ModelResult, Tool, ToolCall


def _env_int(name, default):
    # Tolerate unset OR empty-string env vars (docker-compose passes "" for unset
    # optional vars, and int("") would crash). Fall back to the default.
    try:
        return int(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


class ProviderError(RuntimeError):
    """Transport/provider failure that carries the HTTP status and response body,
    so the agent can show WHY a request was rejected (model name, unsupported
    parameter, wrong endpoint, quota, ...)."""

    def __init__(self, message, *, code=None):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- #
# Portable JSON-text tool protocol (the provider-neutral fallback)
# --------------------------------------------------------------------------- #
def render_tool_protocol(tools: list[Tool]) -> str:
    """Describe the canonical tools as a JSON-text protocol for backends that
    have no (or unreliable) native function calling."""
    lines = [
        "TOOL PROTOCOL",
        "Reply with EXACTLY ONE JSON object per turn and nothing else:",
        '  {"say": "<short note>", "tool": "<name>", "arguments": { ... }}',
        'To answer without a tool, use: {"say": "<answer>", "final": true}',
        "",
        "Available tools:",
    ]
    for t in tools:
        props = (t.input_schema or {}).get("properties", {})
        arg_hint = ", ".join(sorted(props)) or "(none)"
        lines.append(f"- {t.name}: {t.description} | args: {arg_hint}")
    return "\n".join(lines)


def parse_json_object(text: str) -> dict:
    """Hardened single-object JSON extraction: tolerate code fences, prose around
    the object, and stray trailing braces. Raises ValueError if nothing parses."""
    s = (text or "").strip()
    if s.startswith("```"):
        # strip a fenced block (```json ... ```)
        s = s.split("```", 2)[1] if s.count("```") >= 2 else s.strip("`")
        if s.lstrip().lower().startswith("json"):
            s = s.lstrip()[4:]
        s = s.strip()
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    start = s.find("{")
    while start != -1:
        depth = 0
        for i in range(start, len(s)):
            if s[i] == "{":
                depth += 1
            elif s[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(s[start:i + 1])
                        if isinstance(obj, dict) and ("tool" in obj or "say" in obj or "final" in obj):
                            return obj
                    except Exception:
                        break
        start = s.find("{", start + 1)
    raise ValueError("no JSON protocol object found in model output")


def result_from_protocol(obj: dict, *, call_id: str) -> ModelResult:
    say = str(obj.get("say", "") or "")
    tool = obj.get("tool")
    if not tool:
        return ModelResult(say=say, final=bool(obj.get("final", True)))
    args = obj.get("arguments")
    if not isinstance(args, dict):
        args = {}
    return ModelResult(say=say, tool_calls=[ToolCall(id=call_id, name=str(tool), arguments=args)])


# --------------------------------------------------------------------------- #
# Fake adapter (offline / tests)
# --------------------------------------------------------------------------- #
class FakeAdapter:
    """Deterministic adapter for tests and offline dry runs.

    Drive it with either a list of ModelResult (replayed in order) or a callable
    `script(turn) -> ModelResult`. When exhausted it returns a `finish` call.
    """
    supports_native_tools = False

    def __init__(self, script: "list[ModelResult] | Callable[[AgentTurn], ModelResult]"):
        self._script = script
        self._i = 0

    def run_turn(self, turn: AgentTurn) -> ModelResult:
        if callable(self._script):
            return self._script(turn)
        if self._i < len(self._script):
            r = self._script[self._i]
            self._i += 1
            return r
        return ModelResult(say="(fake done)",
                           tool_calls=[ToolCall(id=f"f{self._i}", name="finish",
                                                arguments={"summary": "done"})])


# --------------------------------------------------------------------------- #
# OpenAI-compatible Chat Completions adapter (JSON-text tool mode)
# --------------------------------------------------------------------------- #
class OpenAIChatAdapter:
    """Chat Completions backend using EVA's portable JSON-text tool protocol.

    The canonical event log is rendered into `messages[]`; the model's reply is
    parsed back into a ModelResult. State stays client-side (the event log is the
    source of truth); provider-side conversation state is a future optimisation,
    not a dependency.
    """
    supports_native_tools = False

    def __init__(self, *, endpoint: str, model: str, api_key: str,
                 temperature: "float | None" = None, timeout: int | None = None,
                 transport: "Callable[[str, dict, dict], dict] | None" = None):
        self.endpoint = endpoint
        self.model = model
        self.api_key = api_key
        # temperature is opt-in: several current models reject a non-default value
        # with HTTP 400, so we only send it when explicitly configured.
        self.temperature = temperature
        self.timeout = timeout if timeout is not None else _env_int("LLM_TIMEOUT", 180)
        self._transport = transport or _http_post_json
        self._step = 0

    def _render_messages(self, turn: AgentTurn) -> list[dict]:
        system = turn.system + "\n\n" + render_tool_protocol(turn.tools)
        messages = [{"role": "system", "content": system}]
        for ev in turn.events:
            if ev.role == "system":
                continue
            if ev.role == "user":
                messages.append({"role": "user", "content": ev.content})
            elif ev.role == "assistant":
                if ev.tool_calls:
                    c = ev.tool_calls[0]
                    payload = {"say": ev.content, "tool": c.name, "arguments": c.arguments}
                else:
                    payload = {"say": ev.content, "final": True}
                messages.append({"role": "assistant",
                                 "content": json.dumps(payload, ensure_ascii=False)})
            elif ev.role == "tool":
                label = ev.name or "tool"
                messages.append({"role": "user",
                                 "content": f"OBSERVATION ({label}):\n{ev.content}"})
        return messages

    def run_turn(self, turn: AgentTurn) -> ModelResult:
        self._step += 1
        body = {
            "model": self.model,
            "messages": self._render_messages(turn),
        }
        if self.temperature is not None:
            body["temperature"] = self.temperature
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        data = self._transport(self.endpoint, body, headers)
        text = data["choices"][0]["message"]["content"]
        obj = parse_json_object(text)
        return result_from_protocol(obj, call_id=f"c{self._step}")


def _http_post_json(endpoint: str, body: dict, headers: dict) -> dict:
    req = urllib.request.Request(
        endpoint,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    timeout = _env_int("LLM_TIMEOUT", 180)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode("utf-8", "replace")
        except Exception:
            detail = ""
        raise ProviderError(f"HTTP {e.code}: {detail[:600]}", code=e.code) from None


# --------------------------------------------------------------------------- #
# Factory: explicit provider modes instead of URL heuristics
# --------------------------------------------------------------------------- #
def make_adapter(env: "dict[str, str] | None" = None):
    """Build an adapter from configuration.

        EVA_PROVIDER = fake | openai_chat   (default: openai_chat)
        EVA_MODEL / LLM_MODEL
        EVA_ENDPOINT / LLM_ENDPOINT
        EVA_API_KEY / LLM_API_KEY / OPENAI_API_KEY
        EVA_TEMPERATURE (optional; omitted entirely if unset)
    """
    env = dict(os.environ if env is None else env)
    provider = (env.get("EVA_PROVIDER") or "openai_chat").strip().lower()

    if provider == "fake":
        return FakeAdapter([])

    if provider in ("openai_chat", "openai_compatible_chat", "chat"):
        endpoint = (env.get("EVA_ENDPOINT") or env.get("LLM_ENDPOINT")
                    or "https://api.openai.com/v1/chat/completions")
        model = env.get("EVA_MODEL") or env.get("LLM_MODEL") or ""
        if not model:
            raise SystemExit("No model configured (set EVA_MODEL or LLM_MODEL).")
        key = (env.get("EVA_API_KEY") or env.get("LLM_API_KEY")
               or env.get("OPENAI_API_KEY") or "")
        temp_raw = (env.get("EVA_TEMPERATURE") or "").strip()
        try:
            temperature = float(temp_raw) if temp_raw else None
        except ValueError:
            temperature = None
        return OpenAIChatAdapter(endpoint=endpoint, model=model, api_key=key,
                                 temperature=temperature)

    raise SystemExit(f"Unknown EVA_PROVIDER: {provider!r}")


def llm_error_signature(endpoint: str, exc: Exception) -> str:
    try:
        host = urllib.parse.urlsplit(endpoint).netloc or "llm"
    except Exception:
        host = "llm"
    status = getattr(exc, "code", None)
    if status is None and isinstance(exc, urllib.error.HTTPError):
        status = exc.code
    code = f":{status}" if status is not None else ""
    return f"llm_error:{host}:{type(exc).__name__}{code}"
