"""Base class for specification parsers.

Parsers implement the SpecParser port: they receive an already-loaded dict and
never touch files or the network. Loading lives in stargate.adapters.spec_loader.
"""

import warnings
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, ClassVar

import structlog

from stargate.domain.api import UnifiedAPISchema

logger = structlog.get_logger()


class BaseParser(ABC):
    """Base class for all parsers."""

    format_name: ClassVar[str] = ""

    def __init__(self):
        """Creates parser."""
        self.warnings: list[str] = []
        self.errors: list[str] = []

    @abstractmethod
    def parse(self, spec: dict[str, Any]) -> UnifiedAPISchema:
        """Parses the specification.

        Args:
            spec: Raw specification data

        Returns:
            Unified API schema
        """

    @abstractmethod
    def can_parse(self, spec: dict[str, Any]) -> bool:
        """Checks if this parser can parse the specification.

        Args:
            spec: Raw specification data

        Returns:
            True if can parse
        """

    def parse_file(self, file_path: str | Path) -> UnifiedAPISchema:
        """Deprecated: use ``stargate.parse_spec(path, format=...)`` instead."""
        warnings.warn(
            "stargate.adapters.parsers.base.BaseParser.parse_file is deprecated; "
            "use stargate.parse_spec(path, format=...)",
            DeprecationWarning,
            stacklevel=2,
        )
        from stargate.adapters.spec_loader import FileSpecLoader  # deprecated path only

        return self.parse(FileSpecLoader().load(file_path))

    def parse_url(
        self,
        url: str,
        allowed_domains: set[str] | None = None,
    ) -> UnifiedAPISchema:
        """Deprecated: use ``stargate.parse_spec(url, format=...)`` instead."""
        warnings.warn(
            "stargate.adapters.parsers.base.BaseParser.parse_url is deprecated; "
            "use stargate.parse_spec(url, format=...)",
            DeprecationWarning,
            stacklevel=2,
        )
        from stargate.adapters.spec_loader import UrlSpecLoader  # deprecated path only

        return self.parse(UrlSpecLoader().load(url, allowed_domains))

    def add_warning(self, message: str):
        """Adds warning."""
        self.warnings.append(message)
        logger.warning(message)

    def add_error(self, message: str):
        """Adds error."""
        self.errors.append(message)
        logger.error(message)

    def clear_messages(self):
        """Clears warnings and errors."""
        self.warnings = []
        self.errors = []
