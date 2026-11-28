"""Chat-model port: a model that can be told about tools and ask for them to be run."""

from collections.abc import Sequence
from typing import Protocol

from truejoin.domain.olap.assistant import ChatMessage, ModelTurn, ToolSpec


class ChatModel(Protocol):
    """One turn of a conversation with a model; closed when its owner is."""

    async def chat(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSpec],
        *,
        allow_tools: bool = True,
        timeout: float | None = None,
    ) -> ModelTurn:
        """Send the conversation and return the model's next turn.

        ``allow_tools=False`` keeps the tools declared, so earlier calls still
        read, but asks for an answer in words.
        """
        ...

    async def close(self) -> None:
        """Release provider resources."""
        ...
