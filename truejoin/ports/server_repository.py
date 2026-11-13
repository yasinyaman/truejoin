"""Port: persist and reload MCP server definitions (the server.json format)."""

from pathlib import Path
from typing import Protocol

from truejoin.domain.mcp.mcp import MCPServer


class ServerDefinitionRepository(Protocol):
    """Single owner of how an MCPServer is written to and read from disk."""

    def save(self, server: MCPServer, path: Path) -> Path:
        """Writes the definition. A directory path stores `<dir>/server.json`.

        Returns:
            The file actually written.
        """
        ...

    def load(self, path: Path) -> MCPServer:
        """Reads a definition previously written by save (older layouts included)."""
        ...
