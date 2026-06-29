"""Google Gemini adapter: maps the generic provider seam onto the Gemini REST API.

Like ``media/image.py`` this talks to ``generativelanguage.googleapis.com``
directly (no SDK dependency). It streams ``streamGenerateContent`` server-sent
events from a worker thread and bridges them onto the async seam.

Two Gemini-isms the seam has to absorb:

* **Thinking level.** Gemini 3 exposes reasoning depth as a single discrete dial
  (``thinkingLevel``: minimal < low < medium < high), so the seam's two knobs map
  onto one scale (see ``_thinking_level``): ``/thinking off`` pins the shallowest
  level the model allows (``minimal`` on Flash, ``low`` on 3.x Pro, which rejects
  ``minimal``), the snappy default for a real-time table; ``/thinking on`` lets
  ``effort`` set the depth (medium..high).
* **Thought signatures.** Gemini hands back an opaque ``thoughtSignature`` on
  the parts it wants returned (chiefly function calls) so it can verify
  reasoning continuity across a tool round (the analogue of an Anthropic
  thinking-block signature). We cache them by the synthetic tool-call id (Gemini
  doesn't issue ids) and re-attach them when the engine sends the call back.

See https://ai.google.dev/gemini-api/docs/function-calling
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import AsyncIterator, Iterator
from typing import Any

from openadventure.providers.base import (
    Effort,
    GenerationSettings,
    Message,
    ModelRegistry,
    ProviderError,
    ProviderEvent,
    PTextDelta,
    PThinking,
    PThinkingDelta,
    PToolUse,
    PToolUseStart,
    PTurnDone,
    StopReason,
    SystemBlock,
    ToolDef,
    Usage,
)

GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"

# Gemini's single reasoning dial, shallow -> deep. The seam's two knobs fold onto
# it: thinking off pins the shallowest level; thinking on lets effort pick the
# depth. Gemini's scale tops out at "high" (no "max"), and low/medium effort both
# land on "medium" so /thinking on always deepens past the off-state floor.
_THINKING_SCALE = ("minimal", "low", "medium", "high")
_EFFORT_THINKING_LEVEL = {
    Effort.low: "medium",
    Effort.medium: "medium",
    Effort.high: "high",
    Effort.max: "high",
}


def _thinking_level(settings: GenerationSettings, supported: list[str]) -> str:
    """Map (thinking, effort) onto the deepest ``thinkingLevel`` the model allows.

    thinking off -> the shallowest supported level ("minimal" where the model
    accepts it, else its lowest, e.g. "low" on 3.x Pro). thinking on -> effort
    sets the depth, clamped to what the model supports."""
    desired = (
        _EFFORT_THINKING_LEVEL.get(settings.effort, "high") if settings.thinking else "minimal"
    )
    ranked = sorted((lvl for lvl in supported if lvl in _THINKING_SCALE), key=_THINKING_SCALE.index)
    if not ranked:
        return desired  # model declares no known levels; trust the desired value
    target = _THINKING_SCALE.index(desired)
    at_or_below = [lvl for lvl in ranked if _THINKING_SCALE.index(lvl) <= target]
    return at_or_below[-1] if at_or_below else ranked[0]


# Gemini reports a single STOP for both plain turns and function-calling turns,
# so a tool round is detected from the presence of function calls, not here.
_STOP_REASONS: dict[str | None, StopReason] = {
    "STOP": "end_turn",
    "MAX_TOKENS": "max_tokens",
    "SAFETY": "refusal",
    "RECITATION": "refusal",
    "BLOCKLIST": "refusal",
    "PROHIBITED_CONTENT": "refusal",
    "SPII": "refusal",
}

# JSON-Schema keywords Gemini's function-declaration schema rejects or ignores.
_SCHEMA_DROP = frozenset(
    {"title", "additionalProperties", "$schema", "$defs", "discriminator", "examples"}
)


# --- JSON Schema -> Gemini schema ----------------------------------------
#
# pydantic emits $ref/$defs, const, and Optional-as-anyOf-with-null; Gemini's
# schema dialect accepts none of those, so we inline refs and normalise the rest.


def _gemini_schema(schema: dict[str, Any], defs: dict[str, Any]) -> dict[str, Any]:
    if "$ref" in schema:
        target = schema["$ref"].rsplit("/", 1)[-1]
        resolved = _gemini_schema(defs.get(target, {}), defs)
        # keep any sibling keywords (description, default) alongside the ref
        for key, value in schema.items():
            if key != "$ref" and key not in resolved:
                resolved[key] = value
        return resolved

    out: dict[str, Any] = {}
    for key, value in schema.items():
        if key in _SCHEMA_DROP:
            continue
        if key == "const":
            out["enum"] = [value]
            out.setdefault("type", _json_type(value))
        elif key == "anyOf":
            _merge_any_of(out, [_gemini_schema(s, defs) for s in value])
        elif key == "properties":
            out["properties"] = {k: _gemini_schema(v, defs) for k, v in value.items()}
        elif key == "items":
            out["items"] = _gemini_schema(value, defs)
        else:
            out[key] = value
    return out


def _merge_any_of(out: dict[str, Any], options: list[dict[str, Any]]) -> None:
    """Collapse pydantic's Optional shape (``anyOf: [X, {type: null}]``) into the
    inner schema marked ``nullable``; keep a genuine union as ``anyOf``."""
    non_null = [o for o in options if o.get("type") != "null"]
    if len(non_null) < len(options):
        out["nullable"] = True
    if len(non_null) == 1:
        out.update({k: v for k, v in non_null[0].items() if k not in out})
    elif non_null:
        out["anyOf"] = non_null


def _json_type(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "string"


def _tool_parameters(schema: dict[str, Any]) -> dict[str, Any]:
    defs = schema.get("$defs", {})
    params = _gemini_schema(schema, defs)
    # Gemini wants an object schema even when the tool takes no arguments.
    params.setdefault("type", "object")
    if params["type"] == "object":
        params.setdefault("properties", {})
    return params


class GeminiProvider:
    """Streams the GM agent turn from a Gemini model with minimal thinking."""

    def __init__(self, api_key: str, registry: ModelRegistry | None = None):
        self.api_key = api_key
        self.registry = registry or ModelRegistry.load_default()
        # synthetic tool-call ids -> function name / thought signature, used to
        # rebuild a model turn the engine sends back during a tool loop.
        self._tool_names: dict[str, str] = {}
        self._tool_signatures: dict[str, str] = {}
        self._call_counter = 0

    # --- request construction ---------------------------------------------
    def _convert_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        contents: list[dict[str, Any]] = []
        for message in messages:
            role = "model" if message.role == "assistant" else "user"
            parts: list[dict[str, Any]] = []
            for block in message.content:
                match block.type:
                    case "text":
                        if block.text:
                            parts.append({"text": block.text})
                    case "thinking":
                        part: dict[str, Any] = {"text": block.thinking, "thought": True}
                        if block.signature:
                            part["thoughtSignature"] = block.signature
                        parts.append(part)
                    case "redacted_thinking":
                        # opaque Anthropic payload; nothing to round-trip to Gemini
                        continue
                    case "tool_use":
                        # Remember the call's name so a later tool_result can name its
                        # functionResponse. Live calls also cache this from the stream,
                        # but a replayed history call (synthetic id, never streamed by
                        # this provider) is only known from the block itself.
                        self._tool_names.setdefault(block.id, block.name)
                        call: dict[str, Any] = {
                            "functionCall": {"name": block.name, "args": block.input}
                        }
                        signature = self._tool_signatures.get(block.id)
                        if signature:
                            call["thoughtSignature"] = signature
                        parts.append(call)
                    case "tool_result":
                        name = self._tool_names.get(block.tool_use_id, block.tool_use_id)
                        field = "error" if block.is_error else "result"
                        parts.append(
                            {
                                "functionResponse": {
                                    "name": name,
                                    "response": {field: block.content},
                                }
                            }
                        )
            if parts:
                contents.append({"role": role, "parts": parts})
        return contents

    def _request_body(
        self,
        *,
        system: list[SystemBlock],
        messages: list[Message],
        tools: list[ToolDef],
        settings: GenerationSettings,
    ) -> dict[str, Any]:
        model = self.registry.get(settings.model)
        generation_config: dict[str, Any] = {
            "maxOutputTokens": min(settings.max_tokens, model.max_output),
        }
        if model.supports_thinking:
            # one dial for both knobs, clamped to the model's supported levels.
            generation_config["thinkingConfig"] = {
                "thinkingLevel": _thinking_level(settings, model.thinking_levels)
            }
        body: dict[str, Any] = {
            "contents": self._convert_messages(messages),
            "generationConfig": generation_config,
        }
        system_text = "\n\n".join(b.text for b in system if b.text)
        if system_text:
            body["systemInstruction"] = {"parts": [{"text": system_text}]}
        if tools:
            body["tools"] = [
                {
                    "functionDeclarations": [
                        {
                            "name": t.name,
                            "description": t.description,
                            "parameters": _tool_parameters(t.input_schema),
                        }
                        for t in tools
                    ]
                }
            ]
        return body

    # --- streaming ---------------------------------------------------------
    async def stream_turn(
        self,
        *,
        system: list[SystemBlock],
        messages: list[Message],
        tools: list[ToolDef],
        settings: GenerationSettings,
    ) -> AsyncIterator[ProviderEvent]:
        body = self._request_body(system=system, messages=messages, tools=tools, settings=settings)
        url = f"{GEMINI_API_BASE}/models/{settings.model}:streamGenerateContent?alt=sse"

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue()
        done = object()

        def worker() -> None:
            try:
                for chunk in _stream_chunks(url, body, self.api_key):
                    loop.call_soon_threadsafe(queue.put_nowait, chunk)
            except BaseException as exc:  # surfaced on the loop thread below
                loop.call_soon_threadsafe(queue.put_nowait, exc)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, done)

        task = loop.run_in_executor(None, worker)
        tool_uses: list[PToolUse] = []
        thinking_text = ""
        thinking_signature = ""
        stop_reason: StopReason = "end_turn"
        usage = Usage()
        try:
            while True:
                item = await queue.get()
                if item is done:
                    break
                if isinstance(item, BaseException):
                    raise _wrap_error(item)
                for event in self._handle_chunk(item, tool_uses):
                    if event.type == "thinking":
                        thinking_text += event.thinking
                        thinking_signature = event.signature or thinking_signature
                        # Surface the chunk live for progress display; the whole
                        # block is re-emitted as PThinking once the turn ends.
                        if event.thinking.strip():
                            yield PThinkingDelta(thinking=event.thinking)
                    else:
                        yield event
                candidate = (item.get("candidates") or [{}])[0]
                if candidate.get("finishReason"):
                    stop_reason = _STOP_REASONS.get(candidate["finishReason"], "other")
                if item.get("usageMetadata"):
                    usage = _usage(item["usageMetadata"])
        finally:
            await task

        if thinking_text:
            yield PThinking(thinking=thinking_text, signature=thinking_signature)
        for tool_use in tool_uses:
            yield PToolUse(id=tool_use.id, name=tool_use.name, input=tool_use.input)
        if tool_uses:
            stop_reason = "tool_use"
        yield PTurnDone(stop_reason=stop_reason, usage=usage)

    def _handle_chunk(
        self, chunk: dict[str, Any], tool_uses: list[PToolUse]
    ) -> Iterator[ProviderEvent]:
        for candidate in chunk.get("candidates", []):
            for part in candidate.get("content", {}).get("parts", []):
                if "functionCall" in part:
                    call = part["functionCall"]
                    self._call_counter += 1
                    call_id = f"gem-{self._call_counter}"
                    name = call.get("name", "")
                    self._tool_names[call_id] = name
                    if part.get("thoughtSignature"):
                        self._tool_signatures[call_id] = part["thoughtSignature"]
                    tool_uses.append(
                        PToolUse(id=call_id, name=name, input=dict(call.get("args") or {}))
                    )
                    yield PToolUseStart(id=call_id, name=name)
                elif isinstance(part.get("text"), str):
                    if part.get("thought"):
                        yield PThinking(
                            thinking=part["text"], signature=part.get("thoughtSignature", "")
                        )
                    elif part["text"]:
                        yield PTextDelta(text=part["text"])


def _usage(meta: dict[str, Any]) -> Usage:
    cached = meta.get("cachedContentTokenCount", 0) or 0
    return Usage(
        # promptTokenCount includes cached tokens; split them out like the
        # Anthropic adapter so cost estimation treats a cache read cheaply.
        input_tokens=max((meta.get("promptTokenCount", 0) or 0) - cached, 0),
        output_tokens=(meta.get("candidatesTokenCount", 0) or 0)
        + (meta.get("thoughtsTokenCount", 0) or 0),
        cache_read_input_tokens=cached,
    )


def _stream_chunks(url: str, body: dict[str, Any], api_key: str) -> Iterator[dict[str, Any]]:
    """Yield parsed JSON objects from a Gemini SSE stream (blocking)."""
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": api_key or ""},
        method="POST",
    )
    with urllib.request.urlopen(request) as response:
        for raw in response:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data:"):
                continue
            payload = line[len("data:") :].strip()
            if not payload or payload == "[DONE]":
                continue
            yield json.loads(payload)


def _wrap_error(exc: BaseException) -> ProviderError:
    if isinstance(exc, urllib.error.HTTPError):
        detail = exc.read().decode("utf-8", errors="replace")
        recoverable = exc.code not in (401, 403)
        # 404 unknown model, 413 too large for this model, 429 rate limit,
        # 503 overloaded/unavailable: a different model is a plausible fix.
        suggest_model = exc.code in (404, 413, 429, 503)
        message = f"API error {exc.code}: {detail[:500] or exc.reason}"
        if not recoverable:
            message = "Authentication failed. Check your GEMINI_API_KEY / GOOGLE_API_KEY."
        return ProviderError(message, recoverable=recoverable, suggest_model=suggest_model)
    if isinstance(exc, urllib.error.URLError):
        return ProviderError(f"Could not reach the Gemini API (network error): {exc.reason}")
    if isinstance(exc, ProviderError):
        return exc
    return ProviderError(f"Gemini request failed: {exc}")
