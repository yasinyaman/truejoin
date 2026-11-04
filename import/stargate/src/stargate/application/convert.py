"""Use case: convert an API specification into an MCP server definition."""

from collections.abc import Callable, Sequence
from typing import Any

import structlog

from stargate.application.generators.server import MCPServerGenerator
from stargate.domain.api import HTTPMethod, ServerInfo, UnifiedAPISchema
from stargate.domain.mcp import MCPConversionResult
from stargate.ports.spec_loader import SpecLoader, SpecSource
from stargate.ports.spec_parser import SpecParser

logger = structlog.get_logger()


class ParserRegistry:
    """Known parsers, in detection priority order."""

    FORMAT_ALIASES: dict[str, str] = {"swagger": "openapi"}

    def __init__(self, parsers: Sequence[type[SpecParser]]):
        self._parsers = list(parsers)

    def detect(self, spec: dict[str, Any]) -> SpecParser | None:
        """Returns the first parser that recognizes the document, or None."""
        for parser_class in self._parsers:
            parser = parser_class()
            if parser.can_parse(spec):
                return parser
        return None

    def for_format(self, name: str) -> SpecParser:
        """Returns a parser by format name (case-insensitive, aliases resolved).

        Raises:
            ValueError: unknown format
        """
        key = name.lower()
        key = self.FORMAT_ALIASES.get(key, key)
        for parser_class in self._parsers:
            if parser_class.format_name == key:
                return parser_class()
        raise ValueError(f"Unknown format: {name}")

    def formats(self) -> list[str]:
        """Registered format names."""
        return [parser_class.format_name for parser_class in self._parsers]


class ConversionService:
    """Loads, parses and converts a specification; all I/O goes through ports."""

    def __init__(
        self,
        loader: SpecLoader,
        parsers: ParserRegistry,
        generator_factory: Callable[..., MCPServerGenerator] = MCPServerGenerator,
    ):
        self.loader = loader
        self.parsers = parsers
        self.generator_factory = generator_factory

    def parse(
        self,
        source: SpecSource,
        format: str | None = None,
        allowed_domains: set[str] | None = None,
    ) -> UnifiedAPISchema:
        """Parses a specification without MCP conversion."""
        spec = self.loader.load(source, allowed_domains)
        return self._select_parser(spec, format).parse(spec)

    def convert(
        self,
        source: SpecSource,
        base_url: str | None = None,
        format: str | None = None,
        include_deprecated: bool = False,
        filter_tags: list[str] | None = None,
        filter_methods: list[HTTPMethod] | None = None,
        allowed_domains: set[str] | None = None,
    ) -> MCPConversionResult:
        """Converts a specification into an MCP server definition.

        Raises:
            ValueError: unknown or undetectable format, unsafe URL
            FileNotFoundError: file source does not exist
        """
        logger.info("Starting API conversion", source=str(source)[:100])

        spec = self.loader.load(source, allowed_domains)
        parser = self._select_parser(spec, format)
        schema = parser.parse(spec)

        if base_url and not schema.servers:
            schema.servers = [ServerInfo(url=base_url)]

        generator = self.generator_factory(
            include_deprecated=include_deprecated,
            filter_tags=filter_tags,
            filter_methods=filter_methods,
        )
        result = generator.generate(schema)

        result.warnings.extend(parser.warnings)
        result.errors.extend(parser.errors)

        logger.info(
            "API conversion completed",
            tools=len(result.server.tools),
            resources=len(result.server.resources),
            warnings=len(result.warnings),
        )
        return result

    def _select_parser(self, spec: dict[str, Any], format: str | None) -> SpecParser:
        if format:
            return self.parsers.for_format(format)

        parser = self.parsers.detect(spec)
        if not parser:
            raise ValueError(
                "Could not detect specification format. "
                "Supported formats: OpenAPI 3.x, Postman Collection, Manual"
            )
        return parser
