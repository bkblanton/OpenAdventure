"""Engine -> frontend event types. THE frontend API.

The terminal renderer consumes these today; a browser frontend later consumes
the same stream serialized over SSE/WebSocket (every event is a pydantic
model, so `model_dump_json()` just works).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from openadventure.providers.base import Usage


class TurnStarted(BaseModel):
    type: Literal["turn_started"] = "turn_started"
    turn_id: str


class AssistantTextDelta(BaseModel):
    type: Literal["assistant_text_delta"] = "assistant_text_delta"
    text: str


class DebugChatter(BaseModel):
    type: Literal["debug_chatter"] = "debug_chatter"
    text: str
    reason: str = ""


class ToolStarted(BaseModel):
    type: Literal["tool_started"] = "tool_started"
    call_id: str
    name: str
    args_summary: str = ""


class ToolFinished(BaseModel):
    type: Literal["tool_finished"] = "tool_finished"
    call_id: str
    name: str
    args_summary: str = ""
    result_summary: str = ""
    ok: bool = True
    args: dict = Field(default_factory=dict)  # full payloads for debug mode
    result: str = ""
    private: bool = False


class RollResult(BaseModel):
    type: Literal["roll_result"] = "roll_result"
    expression: str
    total: int
    detail: str
    reason: str | None = None
    private: bool = False
    outcome: str | None = None  # engine-decided check result, e.g. "hard success"
    max_rolls: int = 0  # kept dice at their highest face (natural max): crits, pool hits
    min_rolls: int = 0  # kept dice showing 1 (natural min): fumbles, pool glitches


class StateChanged(BaseModel):
    type: Literal["state_changed"] = "state_changed"
    kind: Literal["sheet", "encounter", "scene", "notes", "canon", "clocks"]
    ref: str
    summary: str = ""
    private: bool = False


class ModuleTransition(BaseModel):
    type: Literal["module_transition"] = "module_transition"
    completed: str
    completed_title: str
    active: str | None = None  # new module in play, or None when the arc is finished
    active_title: str | None = None


class BackgroundTaskStarted(BaseModel):
    type: Literal["background_task_started"] = "background_task_started"
    task_id: str
    kind: str  # "image" | "music" | ...
    label: str


class BackgroundTaskFinished(BaseModel):
    type: Literal["background_task_finished"] = "background_task_finished"
    task_id: str
    ok: bool = True
    message: str = ""


class ImageGenerated(BaseModel):
    type: Literal["image_generated"] = "image_generated"
    path: str
    caption: str = ""


class ShowImage(BaseModel):
    type: Literal["show_image"] = "show_image"
    path: str
    caption: str = ""


class MusicStarted(BaseModel):
    type: Literal["music_started"] = "music_started"
    track: str
    mood: str = ""


class MusicStopped(BaseModel):
    type: Literal["music_stopped"] = "music_stopped"


class TurnCompleted(BaseModel):
    type: Literal["turn_completed"] = "turn_completed"
    turn_id: str
    usage: Usage = Field(default_factory=Usage)
    tool_rounds: int = 0
    prompt_tokens_est: int = 0


class CompactionStarted(BaseModel):
    type: Literal["compaction_started"] = "compaction_started"


class CompactionProgress(BaseModel):
    """A heartbeat tick while the canon chronicler works, emitted as it reasons
    (about one per sentence) so a manual /compact can animate a "still working"
    spinner. It carries no reasoning text: the renderer shows a random flavor
    phrase instead, because the chronicler's thinking may reference GM-only canon."""

    type: Literal["compaction_progress"] = "compaction_progress"


class CompactionFinished(BaseModel):
    type: Literal["compaction_finished"] = "compaction_finished"
    summary_tokens_est: int = 0


class EngineError(BaseModel):
    type: Literal["engine_error"] = "engine_error"
    message: str
    recoverable: bool = True
    suggest_retry: bool = False
    suggest_model: bool = False


EngineEvent = (
    TurnStarted
    | AssistantTextDelta
    | DebugChatter
    | ToolStarted
    | ToolFinished
    | RollResult
    | StateChanged
    | ModuleTransition
    | BackgroundTaskStarted
    | BackgroundTaskFinished
    | ImageGenerated
    | ShowImage
    | MusicStarted
    | MusicStopped
    | TurnCompleted
    | CompactionStarted
    | CompactionProgress
    | CompactionFinished
    | EngineError
)
