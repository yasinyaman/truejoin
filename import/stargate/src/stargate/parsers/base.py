"""Base parser class."""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import structlog
import yaml

from stargate.models.unified import UnifiedAPISchema
from stargate.utils.http import fetch_spec_sync

logger = structlog.get_logger()


class BaseParser(ABC):
    """Base class for all parsers."""

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
        pass

    @abstractmethod
    def can_parse(self, spec: dict[str, Any]) -> bool:
        """Checks if this parser can parse the specification.

        Args:
            spec: Raw specification data

        Returns:
            True if can parse
        """
        pass

    def parse_file(self, file_path: str | Path) -> UnifiedAPISchema:
        """Parses specification from file.

        Args:
            file_path: File path

        Returns:
            Unified API schema

        Raises:
            FileNotFoundError: File not found
            ValueError: Invalid file format
        """
        path = Path(file_path)

        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")

        logger.info("Parsing file", path=str(path))

        content = path.read_text(encoding="utf-8")

        if path.suffix in (".yaml", ".yml"):
            spec = yaml.safe_load(content)
        elif path.suffix == ".json":
            import json

            spec = json.loads(content)
        else:
            # Try JSON first, if fails try YAML
            try:
                import json

                spec = json.loads(content)
            except json.JSONDecodeError:
                spec = yaml.safe_load(content)

        if not isinstance(spec, dict):
            raise ValueError(f"Invalid specification format: {type(spec)}")

        return self.parse(spec)

    def parse_url(
        self,
        url: str,
        allowed_domains: set[str] | None = None,
    ) -> UnifiedAPISchema:
        """Parses specification from URL.

        Args:
            url: Specification URL
            allowed_domains: Allowed domains

        Returns:
            Unified API schema
        """
        logger.info("Parsing from URL", url=url)

        spec = fetch_spec_sync(url, allowed_domains)

        if not isinstance(spec, dict):
            raise ValueError(f"Invalid specification format: {type(spec)}")

        return self.parse(spec)

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
