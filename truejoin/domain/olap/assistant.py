"""What the built-in assistant and a chat model say to each other.

The assistant is a model that answers a question by calling the analytical
tools, one turn at a time. These are the words of that exchange, kept free of
any provider's shape: an adapter turns them into its own request and back.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal

Role = Literal["system", "user", "assistant", "tool"]


@dataclass(frozen=True)
class ToolSpec:
    """One tool as a model is told about it: its name, what it does, its JSON Schema."""

    name: str
    description: str
    parameters: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ToolRequest:
    """A model asking for one tool to be run.

    ``signature`` is whatever a provider wants back with the call when the
    conversation is sent again (opaque here). ``error`` is set when the
    arguments the model wrote could not be read: the call is answered with it
    and nothing runs.
    """

    id: str
    name: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    signature: str = ""
    error: str = ""


@dataclass(frozen=True)
class ChatMessage:
    """One message of the exchange.

    An ``assistant`` message carries text, tool requests, or both. A ``tool``
    message answers one request: ``tool_call_id`` and ``name`` say which.
    ``provider_state`` is a turn exactly as its provider gave it, for the
    provider that wrote it to send back unchanged (its reasoning blocks, which
    some providers require to be returned as they came). Opaque to everyone else.
    """

    role: Role
    text: str = ""
    tool_calls: tuple[ToolRequest, ...] = ()
    tool_call_id: str = ""
    name: str = ""
    provider_state: str = ""


@dataclass(frozen=True)
class ModelTurn:
    """What a model answered: text, tool requests, or both; token counts when it reports them."""

    text: str = ""
    tool_calls: tuple[ToolRequest, ...] = ()
    input_tokens: int | None = None
    output_tokens: int | None = None
    #: The turn as the provider gave it, to be sent back unchanged (see ChatMessage).
    provider_state: str = ""
    #: Why the model stopped, in the provider's own word ("end_turn", "max_tokens", ...).
    stop: str = ""

    def as_message(self) -> ChatMessage:
        """This turn as the assistant message the next request carries."""
        return ChatMessage(
            role="assistant",
            text=self.text,
            tool_calls=self.tool_calls,
            provider_state=self.provider_state,
        )


@dataclass(frozen=True)
class Exchange:
    """An earlier question of the conversation and the answer it got."""

    question: str
    answer: str


@dataclass(frozen=True)
class AssistantLimits:
    """What bounds one run: turns, time, how much of the past and of a result it carries."""

    max_steps: int = 8
    timeout: float = 120.0
    history_turns: int = 6
    max_concurrent: int = 4
    #: Characters of one tool result given back to the model.
    result_chars: int = 12_000
    #: Characters of earlier questions and answers sent along, newest kept.
    history_chars: int = 8_000
    #: Characters of the question itself.
    question_chars: int = 4_000


@dataclass(frozen=True)
class StepEvent:
    """A tool the model asked for (``phase`` "call"), then how it went ("result")."""

    index: int
    phase: Literal["call", "result"]
    tool: str
    arguments: Mapping[str, Any] = field(default_factory=dict)
    outcome: str = ""
    row_count: int | None = None
    duration_ms: float = 0.0
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        """The step as a flat JSON-safe mapping."""
        out: dict[str, Any] = {
            "type": "step",
            "index": self.index,
            "phase": self.phase,
            "tool": self.tool,
            "arguments": dict(self.arguments),
        }
        if self.phase == "result":
            out.update(
                outcome=self.outcome,
                row_count=self.row_count,
                duration_ms=round(self.duration_ms, 1),
                error=self.error,
            )
        return out


@dataclass(frozen=True)
class AnswerEvent:
    """The answer: its words, the statements that ran and the last rows read.

    ``outcome`` is "answered", or "exhausted" when the model was out of steps
    and answered from what it had.
    """

    text: str
    outcome: Literal["answered", "exhausted"] = "answered"
    sql: tuple[str, ...] = ()
    result: Mapping[str, Any] | None = None
    steps: int = 0
    tool_calls: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None

    def as_dict(self) -> dict[str, Any]:
        """The answer as a JSON-safe mapping."""
        return {
            "type": "answer",
            "outcome": self.outcome,
            "text": self.text,
            "sql": list(self.sql),
            "result": dict(self.result) if self.result is not None else None,
            "steps": self.steps,
            "tool_calls": self.tool_calls,
            "usage": {"input_tokens": self.input_tokens, "output_tokens": self.output_tokens},
        }


#: Why a run ended without an answer.
ErrorCode = Literal["busy", "timeout", "model", "stopped"]


@dataclass(frozen=True)
class ErrorEvent:
    """A run that ended without an answer, and why."""

    code: ErrorCode
    message: str

    def as_dict(self) -> dict[str, Any]:
        """The failure as a JSON-safe mapping."""
        return {"type": "error", "code": self.code, "message": self.message}


AssistantEvent = StepEvent | AnswerEvent | ErrorEvent


def clip_result(result: Mapping[str, Any], budget: int) -> str:
    """A tool's result as the JSON a model is given back, held to ``budget`` characters.

    Rows are dropped from the end until it fits, and the result says how many
    it kept, so the model knows it is reading a part. A result too long without
    rows (a wide table's description) is given by its beginning.
    """
    text = json.dumps(result, default=str, ensure_ascii=False)
    if len(text) <= budget:
        return text
    rows = result.get("rows")
    if isinstance(rows, list) and rows:
        kept = len(rows)
        while kept > 1:
            kept //= 2
            trial = {
                **result,
                "rows": rows[:kept],
                "clipped": f"Only the first {kept} of {len(rows)} rows are given here: "
                "ask for less (an aggregate, fewer columns, a condition) to read the rest.",
            }
            text = json.dumps(trial, default=str, ensure_ascii=False)
            if len(text) <= budget:
                return text
    whole = json.dumps(result, default=str, ensure_ascii=False)
    return json.dumps(
        {
            "clipped": "The result was too long to give whole: this is its beginning.",
            "beginning": whole[: max(0, budget - 200)],
        },
        ensure_ascii=False,
    )
