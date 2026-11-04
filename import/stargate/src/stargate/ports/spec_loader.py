"""Port: obtain a raw specification document from some source."""

from pathlib import Path
from typing import Any, Protocol

SpecSource = str | Path | dict[str, Any]


class SpecLoader(Protocol):
    """Resolves a source (URL, file path or in-memory dict) to a raw spec dict."""

    def supports(self, source: SpecSource) -> bool:
        """Returns True if this loader can handle the given source."""
        ...

    def load(self, source: SpecSource, allowed_domains: set[str] | None = None) -> dict[str, Any]:
        """Loads the document.

        Raises:
            FileNotFoundError: source is a path that does not exist
            ValueError: unsafe URL, unsupported source or undecodable content
        """
        ...
