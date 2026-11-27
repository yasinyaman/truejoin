"""What the built-in assistant and a chat model say to each other.

The assistant is a model that answers a question by calling the analytical
tools, one turn at a time. These are the words of that exchange, kept free of
any provider's shape: an adapter turns them into its own request and back.
"""

from __future__ import annotations

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
    """

    role: Role
    text: str = ""
    tool_calls: tuple[ToolRequest, ...] = ()
    tool_call_id: str = ""
    name: str = ""


@dataclass(frozen=True)
class ModelTurn:
    """What a model answered: text, tool requests, or both; token counts when it reports them."""

    text: str = ""
    tool_calls: tuple[ToolRequest, ...] = ()
    input_tokens: int | None = None
    output_tokens: int | None = None

    def as_message(self) -> ChatMessage:
        """This turn as the assistant message the next request carries."""
        return ChatMessage(role="assistant", text=self.text, tool_calls=self.tool_calls)
