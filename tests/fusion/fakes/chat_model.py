"""A chat model that says what a script tells it to, and keeps what it was sent."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from truejoin.domain.olap.assistant import ChatMessage, ModelTurn, ToolRequest, ToolSpec

Turn = ModelTurn | BaseException | Callable[[Sequence[ChatMessage]], ModelTurn]


def ask(tool: str, call_id: str = "", **arguments: Any) -> ModelTurn:
    """A turn that asks for one tool to be run."""
    return ModelTurn(tool_calls=(ToolRequest(call_id or f"call-{tool}", tool, arguments),))


def say(text: str, input_tokens: int | None = None, output_tokens: int | None = None) -> ModelTurn:
    """A turn that answers in words."""
    return ModelTurn(text=text, input_tokens=input_tokens, output_tokens=output_tokens)


class ScriptedChat:
    """The sync callable ``AssistantService`` talks to: one scripted turn per call.

    A turn is a :class:`ModelTurn`, an exception to raise, or a function of the
    messages (to answer from what a tool returned). Past the script's end it
    keeps giving the last turn, which is how a model that never stops is said.
    """

    def __init__(self, *turns: Turn) -> None:
        self.turns = list(turns)
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        allow_tools: bool,
        timeout: float,
    ) -> ModelTurn:
        self.calls.append(
            {
                "messages": tuple(messages),
                "tools": tuple(tool.name for tool in tools),
                "allow_tools": allow_tools,
                "timeout": timeout,
            }
        )
        turn = self.turns[min(len(self.calls), len(self.turns)) - 1]
        if isinstance(turn, BaseException):
            raise turn
        return turn(messages) if callable(turn) else turn

    @property
    def sent(self) -> tuple[ChatMessage, ...]:
        """The messages of the last call."""
        return self.calls[-1]["messages"]

    def results(self) -> list[str]:
        """What the tools answered, as the model was given it, in order."""
        return [m.text for m in self.sent if m.role == "tool"]


class ScriptedChatModel:
    """The same script behind the async port, for wiring a whole server to it."""

    def __init__(self, *turns: Turn) -> None:
        self.script = ScriptedChat(*turns)
        self.closed = False

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        *,
        allow_tools: bool = True,
        timeout: float | None = None,
    ) -> ModelTurn:
        return self.script(messages, tools, allow_tools, timeout or 0.0)

    async def close(self) -> None:
        self.closed = True
