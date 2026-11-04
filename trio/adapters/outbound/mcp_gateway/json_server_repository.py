"""Adapter: server.json persistence (the single owner of the on-disk layout).

Layout (version 1): the MCPServer model dumped with wire aliases
(``inputSchema``, ``mimeType``) plus a ``stargate_format`` marker. Files written
by earlier versions (no marker, no ``parameter_locations``) load unchanged.
"""

import json
from pathlib import Path
from typing import Any

from trio.domain.mcp.mcp import MCPServer

FORMAT_VERSION = 1
DEFAULT_FILENAME = "server.json"


def _drop_none(data: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in data.items() if value is not None}


class JsonServerRepository:
    """Reads and writes MCPServer definitions as JSON files."""

    def resolve_path(self, path: Path) -> Path:
        """A directory (existing, or extension-less) maps to ``<dir>/server.json``."""
        path = Path(path)
        if path.is_dir() or not path.suffix:
            return path / DEFAULT_FILENAME
        return path

    def to_document(self, server: MCPServer) -> dict[str, Any]:
        """Serializable form of a server definition."""
        data = server.model_dump(mode="json", by_alias=True)
        data["tools"] = [_drop_none(tool) for tool in data["tools"]]
        data["resources"] = [_drop_none(resource) for resource in data["resources"]]
        return {"stargate_format": FORMAT_VERSION, **_drop_none(data)}

    def from_document(self, data: dict[str, Any]) -> MCPServer:
        """Builds a server definition; unknown keys are ignored, aliases and names both accepted."""
        return MCPServer.model_validate(data)

    def save(self, server: MCPServer, path: Path) -> Path:
        """Writes the definition and returns the file written."""
        target = self.resolve_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_document(server), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return target

    def load(self, path: Path) -> MCPServer:
        """Reads a definition.

        Raises:
            FileNotFoundError: file does not exist
        """
        path = Path(path)
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        return self.from_document(json.loads(path.read_text(encoding="utf-8")))
