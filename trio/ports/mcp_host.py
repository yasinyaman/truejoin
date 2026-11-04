"""Port: expose tools and resources over an MCP transport."""

from collections.abc import Awaitable, Callable
from typing import Any, Protocol

from trio.domain.mcp.mcp import MCPResource, MCPTool

ToolHandler = Callable[[dict[str, Any]], Awaitable[Any]]
"""Receives the tool arguments as a dict; returns the tool result."""

ResourceHandler = Callable[[dict[str, str]], Awaitable[str]]
"""Receives URI template parameters (empty for static resources); returns content."""


class MCPHost(Protocol):
    """An MCP server framework binding (e.g. the official MCP SDK server)."""

    def register_tool(self, tool: MCPTool, handler: ToolHandler) -> None:
        """Publishes a tool whose invocations are routed to `handler`."""
        ...

    def register_resource(self, resource: MCPResource, handler: ResourceHandler) -> None:
        """Publishes a resource (or resource template when the URI has {params})."""
        ...

    def run(self, transport: str = "stdio") -> None:
        """Blocks serving the given transport."""
        ...
