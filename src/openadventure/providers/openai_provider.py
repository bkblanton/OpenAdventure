"""OpenAI adapter: maps the generic provider seam onto the OpenAI Responses API.

Like ``gemini_provider.py`` (and ``media/image.py``) this talks to the REST API
directly over ``urllib`` (no SDK dependency): it streams the Responses API's
server-sent events from a worker thread and bridges them onto the async seam.

Three OpenAI-isms the seam has to absorb:

* **Responses API shape.** OpenAI's newer models speak the Responses API, not
  Chat Completions: the system prompt rides in ``instructions``, the transcript is
  a flat list of typed ``input`` items (``message`` / ``function_call`` /
  ``function_call_output`` / ``reasoning``), and a tool call carries an opaque
  ``call_id`` we reuse as the seam's tool-use id so results match back up.
* **Reasoning effort.** GPT-5-class models expose one ``reasoning.effort`` dial
  (minimal < low < medium < high). The seam's two knobs fold onto it (see
  ``_reasoning_effort``): ``/thinking off`` pins ``minimal`` (the snappy real-time
  floor); ``/thinking on`` lets ``effort`` set the depth (low..high, ``max``->high).
* **Reasoning round-trip.** With ``store: false`` we ask for
  ``reasoning.encrypted_content`` and hand the opaque reasoning items back on the
  next tool round so the model keeps its chain of thought across the round (the
  analogue of an Anthropic thinking signature or a Gemini thought signature). We
  stash the item id + encrypted blob in the seam's thinking-block signature and
  rebuild the reasoning item from it when the engine replays the turn.

See https://developers.openai.com/api/docs/models and the Responses API docs.
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

OPENAI_API_BASE = "https://api.openai.com/v1"

# OpenAI's single reasoning dial, shallow -> deep. The seam's two knobs fold onto
# it: thinking off pins "minimal" (the snappy real-time floor); thinking on lets
# effort pick the depth. OpenAI's scale tops out at "high" (no "max"), so max
# effort lands on "high".
_EFFORT_REASONING = {
    Effort.low: "low",
    Effort.medium: "medium",
    Effort.high: "high",
    Effort.max: "high",
}


def _reasoning_effort(settings: GenerationSettings) -> str:
    """Map (thinking, effort) onto a Responses API ``reasoning.effort`` level.

    thinking off -> "minimal" (fastest, barely reasons); thinking on -> effort
    sets the depth (low/medium/high, max clamped to high)."""
    if not settings.thinking:
        return "minimal"
    return _EFFORT_REASONING.get(settings.effort, "medium")


# The Responses API reports how the turn ended via the response's status plus,
# when it stops short, an ``incomplete_details.reason``. A turn that emitted tool
# calls is detected from the presence of function_call items, not from here.
def _stop_reason(response: dict[str, Any]) -> StopReason:
    status = response.get("status")
    if status == "incomplete":
        reason = (response.get("incomplete_details") or {}).get("reason")
        if reason == "max_output_tokens":
            return "max_tokens"
        if reason == "content_filter":
            return "refusal"
        return "other"
    if status == "completed":
        return "end_turn"
    return "other"


class OpenAIProvider:
    """Streams the GM agent turn from an OpenAI Responses-API model."""

    def __init__(self, api_key: str, registry: ModelRegistry | None = None):
        self.api_key = api_key
        self.registry = registry or ModelRegistry.load_default()

    # --- request construction ---------------------------------------------
    def _convert_messages(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Flatten the seam's messages into the Responses API's typed input items.

        Each thinking / text / tool-use / tool-result block becomes its own input
        item; a reasoning block whose signature carries an encrypted blob is
        replayed as a ``reasoning`` item so the model keeps its chain of thought
        across a tool round."""
        items: list[dict[str, Any]] = []
        for message in messages:
            for block in message.content:
                match block.type:
                    case "text":
                        if not block.text:
                            continue
                        part = "output_text" if message.role == "assistant" else "input_text"
                        items.append(
                            {
                                "type": "message",
                                "role": message.role,
                                "content": [{"type": part, "text": block.text}],
                            }
                        )
                    case "thinking":
                        item = _reasoning_item(block.thinking, block.signature)
                        if item is not None:
                            items.append(item)
                    case "redacted_thinking":
                        # opaque Anthropic payload; nothing to round-trip to OpenAI
                        continue
                    case "tool_use":
                        items.append(
                            {
                                "type": "function_call",
                                "call_id": block.id,
                                "name": block.name,
                                "arguments": json.dumps(block.input),
                            }
                        )
                    case "tool_result":
                        items.append(
                            {
                                "type": "function_call_output",
                                "call_id": block.tool_use_id,
                                "output": block.content,
                            }
                        )
        return items

    def _request_body(
        self,
        *,
        system: list[SystemBlock],
        messages: list[Message],
        tools: list[ToolDef],
        settings: GenerationSettings,
    ) -> dict[str, Any]:
        model = self.registry.get(settings.model)
        body: dict[str, Any] = {
            "model": settings.model,
            "input": self._convert_messages(messages),
            "max_output_tokens": min(settings.max_tokens, model.max_output),
            "stream": True,
            # Stateless: we resend the whole transcript each turn (like the other
            # adapters) rather than chain server-side response ids. Asking for the
            # encrypted reasoning lets us still round-trip the chain of thought.
            "store": False,
            "parallel_tool_calls": True,
        }
        system_text = "\n\n".join(b.text for b in system if b.text)
        if system_text:
            body["instructions"] = system_text
        if model.supports_thinking:
            body["reasoning"] = {"effort": _reasoning_effort(settings), "summary": "auto"}
            body["include"] = ["reasoning.encrypted_content"]
        # GPT-5-class models expose an output-length dial that maps cleanly onto
        # the seam's verbosity knob; harmless on models that ignore it.
        body["text"] = {"verbosity": settings.verbosity.value}
        if tools:
            body["tools"] = [
                {
                    "type": "function",
                    "name": t.name,
                    "description": t.description,
                    # Responses accepts standard JSON Schema (incl. $ref/$defs), so
                    # the pydantic schema passes through untouched; non-strict keeps
                    # it lenient about optional fields the way the other backends are.
                    "parameters": t.input_schema,
                    "strict": False,
                }
                for t in tools
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
        url = f"{OPENAI_API_BASE}/responses"

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[Any] = asyncio.Queue()
        done = object()

        def worker() -> None:
            try:
                for event in _stream_events(url, body, self.api_key):
                    loop.call_soon_threadsafe(queue.put_nowait, event)
            except BaseException as exc:  # surfaced on the loop thread below
                loop.call_soon_threadsafe(queue.put_nowait, exc)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, done)

        task = loop.run_in_executor(None, worker)
        stop_reason: StopReason = "end_turn"
        usage = Usage()
        tool_uses: list[PToolUse] = []
        reasoning: list[PThinking] = []
        try:
            while True:
                item = await queue.get()
                if item is done:
                    break
                if isinstance(item, BaseException):
                    raise _wrap_error(item)
                event_type = item.get("type", "")
                if event_type == "response.output_text.delta":
                    if item.get("delta"):
                        yield PTextDelta(text=item["delta"])
                elif event_type == "response.reasoning_summary_text.delta":
                    # Surface reasoning live for progress display; the completed
                    # reasoning item is re-emitted as PThinking once the turn ends.
                    if item.get("delta"):
                        yield PThinkingDelta(thinking=item["delta"])
                elif event_type == "response.output_item.added":
                    added = item.get("item") or {}
                    if added.get("type") == "function_call":
                        yield PToolUseStart(id=added.get("call_id", ""), name=added.get("name", ""))
                elif event_type == "response.completed":
                    response = item.get("response") or {}
                    stop_reason = _stop_reason(response)
                    usage = _usage(response.get("usage") or {})
                    _collect_output(response.get("output") or [], tool_uses, reasoning)
                elif event_type in ("response.failed", "error"):
                    raise _response_error(item)
        finally:
            await task

        for think in reasoning:
            yield think
        for tool_use in tool_uses:
            yield tool_use
        if tool_uses:
            stop_reason = "tool_use"
        yield PTurnDone(stop_reason=stop_reason, usage=usage)


def _collect_output(
    output: list[dict[str, Any]],
    tool_uses: list[PToolUse],
    reasoning: list[PThinking],
) -> None:
    """Pull the completed reasoning and function-call items out of a finished
    response so the turn can round-trip its chain of thought and dispatch tools.

    Reasoning leads the tool calls (as the agent loop rebuilds the turn) so the
    model's chain of thought precedes the calls it produced on the next round."""
    for item in output:
        if item.get("type") == "reasoning":
            summary = "".join(
                part.get("text", "")
                for part in item.get("summary") or []
                if part.get("type") == "summary_text"
            )
            signature = _reasoning_signature(item.get("id", ""), item.get("encrypted_content"))
            if summary or signature:
                reasoning.append(PThinking(thinking=summary, signature=signature))
        elif item.get("type") == "function_call":
            try:
                args = json.loads(item.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_uses.append(
                PToolUse(
                    id=item.get("call_id", ""),
                    name=item.get("name", ""),
                    input=args if isinstance(args, dict) else {},
                )
            )


# --- reasoning-item round-trip -------------------------------------------
#
# The seam only has one opaque slot per thinking block (``signature``), so we pack
# the reasoning item's id and encrypted blob into it as JSON and rebuild the item
# on the way back. A block without an encrypted blob (e.g. reasoning wasn't
# requested) round-trips as summary-only text and is simply dropped from the input.


def _reasoning_signature(item_id: str, encrypted_content: str | None) -> str:
    if not (item_id and encrypted_content):
        return ""
    return json.dumps({"id": item_id, "enc": encrypted_content})


def _reasoning_item(summary: str, signature: str) -> dict[str, Any] | None:
    """Rebuild a Responses API reasoning item from a seam thinking block, or None
    when the block carries no replayable (encrypted) reasoning."""
    if not signature:
        return None
    try:
        decoded = json.loads(signature)
    except json.JSONDecodeError:
        return None
    item_id = decoded.get("id")
    encrypted = decoded.get("enc")
    if not (item_id and encrypted):
        return None
    item: dict[str, Any] = {"type": "reasoning", "id": item_id, "encrypted_content": encrypted}
    item["summary"] = [{"type": "summary_text", "text": summary}] if summary else []
    return item


def _usage(meta: dict[str, Any]) -> Usage:
    cached = (meta.get("input_tokens_details") or {}).get("cached_tokens", 0) or 0
    return Usage(
        # input_tokens counts cached tokens too; split them out like the other
        # adapters so cost estimation reads a cache hit cheaply. OpenAI's
        # output_tokens already includes reasoning tokens, and there is no separate
        # cache-write charge, so cache_creation stays zero.
        input_tokens=max((meta.get("input_tokens", 0) or 0) - cached, 0),
        output_tokens=meta.get("output_tokens", 0) or 0,
        cache_read_input_tokens=cached,
    )


def _stream_events(url: str, body: dict[str, Any], api_key: str) -> Iterator[dict[str, Any]]:
    """Yield parsed JSON objects from an OpenAI Responses SSE stream (blocking).

    The Responses API tags each event with an ``event:`` line and a ``data:`` line;
    the JSON payload already carries a ``type`` field, so we key off that and only
    need to parse the data lines."""
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key or ''}",
        },
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


def _response_error(event: dict[str, Any]) -> ProviderError:
    """A ``response.failed`` / ``error`` stream event -> a surfaced ProviderError."""
    detail = event.get("response") or event
    error = detail.get("error") or {}
    message = error.get("message") or event.get("message") or "OpenAI stream failed."
    # rate limits / overload are retryable and a different model may help.
    code = error.get("code") or event.get("code") or ""
    suggest_model = code in ("rate_limit_exceeded", "server_error")
    return ProviderError(f"OpenAI error: {message}", suggest_model=suggest_model)


def _wrap_error(exc: BaseException) -> ProviderError:
    if isinstance(exc, urllib.error.HTTPError):
        detail = exc.read().decode("utf-8", errors="replace")
        recoverable = exc.code not in (401, 403)
        # 404 unknown model, 413 too large for this model, 429 rate limit,
        # 500/503 overloaded/unavailable: a different model is a plausible fix.
        suggest_model = exc.code in (404, 413, 429, 500, 503)
        message = f"API error {exc.code}: {detail[:500] or exc.reason}"
        if not recoverable:
            message = "Authentication failed. Check your OPENAI_API_KEY."
        return ProviderError(message, recoverable=recoverable, suggest_model=suggest_model)
    if isinstance(exc, urllib.error.URLError):
        return ProviderError(f"Could not reach the OpenAI API (network error): {exc.reason}")
    if isinstance(exc, ProviderError):
        return exc
    return ProviderError(f"OpenAI request failed: {exc}")
