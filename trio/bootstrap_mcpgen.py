"""Composition root: default wiring of adapters into the application services.

Besides trio.runtime_mcpgen this is the only module that knows concrete adapters.
Library users call convert_to_mcp / parse_spec; embedders can assemble their
own ConversionService with different ports.
"""

from typing import Any

from trio.adapters.outbound.mcp_gateway.json_server_repository import JsonServerRepository
from trio.adapters.outbound.mcp_gateway.parsers.manual import ManualParser
from trio.adapters.outbound.mcp_gateway.parsers.openapi import OpenAPIParser
from trio.adapters.outbound.mcp_gateway.parsers.postman import PostmanParser
from trio.adapters.outbound.mcp_gateway.spec_loader import default_spec_loader
from trio.application.mcpgen.convert import ConversionService, ParserRegistry
from trio.domain.mcp.api import HTTPMethod, UnifiedAPISchema
from trio.domain.mcp.mcp import MCPConversionResult
from trio.ports.server_repository import ServerDefinitionRepository
from trio.ports.spec_loader import SpecLoader, SpecSource
from trio.ports.spec_parser import SpecParser

DEFAULT_PARSERS: tuple[type[SpecParser], ...] = (OpenAPIParser, PostmanParser, ManualParser)

# Mutable registration list (in priority order); appending a parser class here
# makes it available to the default service. Kept for stargate.converter compat.
PARSERS: list[type[SpecParser]] = list(DEFAULT_PARSERS)


def default_registry() -> ParserRegistry:
    """Registry over PARSERS."""
    return ParserRegistry(PARSERS)


def default_loader() -> SpecLoader:
    """Loader accepting dicts, URLs and file paths."""
    return default_spec_loader()


def default_service() -> ConversionService:
    """ConversionService with the default loader and parsers."""
    return ConversionService(loader=default_loader(), parsers=default_registry())


def default_repository() -> ServerDefinitionRepository:
    """Repository owning the server.json layout."""
    return JsonServerRepository()


def convert_to_mcp(
    source: SpecSource,
    base_url: str | None = None,
    format: str | None = None,
    include_deprecated: bool = False,
    filter_tags: list[str] | None = None,
    filter_methods: list[HTTPMethod] | None = None,
    allowed_domains: set[str] | None = None,
) -> MCPConversionResult:
    """Converts REST API definition to MCP Server.

    Args:
        source: API definition source (URL, file path or dict)
        base_url: API base URL (if not in specification)
        format: Force format ("openapi", "postman", "manual")
        include_deprecated: Include deprecated endpoints
        filter_tags: Only include endpoints with these tags
        filter_methods: Only turn these HTTP methods into tools
        allowed_domains: Allowed domains (SSRF protection)

    Returns:
        MCPConversionResult (server, metadata, warnings, errors)

    Raises:
        ValueError: Invalid source or format
        FileNotFoundError: File not found

    Example:
        >>> result = convert_to_mcp("https://petstore3.swagger.io/api/v3/openapi.json")
        >>> print(f"Tools: {len(result.server.tools)}")
        >>> result = convert_to_mcp("./api-spec.yaml")
        >>> result = convert_to_mcp({"openapi": "3.0.0", "info": {...}, "paths": {...}})
    """
    return default_service().convert(
        source,
        base_url=base_url,
        format=format,
        include_deprecated=include_deprecated,
        filter_tags=filter_tags,
        filter_methods=filter_methods,
        allowed_domains=allowed_domains,
    )


def parse_spec(
    source: SpecSource,
    format: str | None = None,
    allowed_domains: set[str] | None = None,
) -> UnifiedAPISchema:
    """Parses API specification (without MCP conversion).

    Args:
        source: API definition source (URL, file path or dict)
        format: Force format
        allowed_domains: Allowed domains

    Returns:
        UnifiedAPISchema
    """
    return default_service().parse(source, format=format, allowed_domains=allowed_domains)


def detect_format(spec: dict[str, Any]) -> SpecParser | None:
    """Detects specification format and returns an appropriate parser, or None."""
    return default_registry().detect(spec)
