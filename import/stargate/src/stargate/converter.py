"""Main converter module."""

from pathlib import Path
from typing import Any

import structlog

from stargate.generators.mcp_server import MCPServerGenerator
from stargate.models.mcp import MCPConversionResult
from stargate.models.unified import UnifiedAPISchema
from stargate.parsers.base import BaseParser
from stargate.parsers.manual import ManualParser
from stargate.parsers.openapi import OpenAPIParser
from stargate.parsers.postman import PostmanParser
from stargate.utils.http import fetch_spec_sync

logger = structlog.get_logger()

# Registered parsers (in priority order)
PARSERS: list[type[BaseParser]] = [
    OpenAPIParser,
    PostmanParser,
    ManualParser,
]


def detect_format(spec: dict[str, Any]) -> BaseParser | None:
    """Detects specification format and returns appropriate parser.

    Args:
        spec: Raw specification data

    Returns:
        Appropriate parser or None
    """
    for parser_class in PARSERS:
        parser = parser_class()
        if parser.can_parse(spec):
            return parser
    return None


def convert_to_mcp(
    source: str | Path | dict[str, Any],
    base_url: str | None = None,
    format: str | None = None,
    include_deprecated: bool = False,
    filter_tags: list[str] | None = None,
    allowed_domains: set[str] | None = None,
) -> MCPConversionResult:
    """Converts REST API definition to MCP Server.

    Args:
        source: API definition source (URL, file path or dict)
        base_url: API base URL (if not in specification)
        format: Force format ("openapi", "postman", "manual")
        include_deprecated: Include deprecated endpoints
        filter_tags: Only include endpoints with these tags
        allowed_domains: Allowed domains (SSRF protection)

    Returns:
        MCPConversionResult (server, metadata, warnings, errors)

    Raises:
        ValueError: Invalid source or format
        FileNotFoundError: File not found

    Example:
        >>> # From URL
        >>> result = convert_to_mcp(
        ...     "https://petstore3.swagger.io/api/v3/openapi.json"
        ... )
        >>> print(f"Tools: {len(result.server.tools)}")

        >>> # From file
        >>> result = convert_to_mcp("./api-spec.yaml")

        >>> # From dict
        >>> result = convert_to_mcp({
        ...     "openapi": "3.0.0",
        ...     "info": {"title": "My API", "version": "1.0.0"},
        ...     "paths": {...}
        ... })
    """
    logger.info("Starting API conversion", source=str(source)[:100])

    # Load source
    spec = _load_source(source, allowed_domains)

    # Select parser
    parser = _get_parser(spec, format)

    # Parse
    schema = parser.parse(spec)

    # Base URL override
    if base_url and not schema.servers:
        from stargate.models.unified import ServerInfo

        schema.servers = [ServerInfo(url=base_url)]

    # Create MCP Server
    generator = MCPServerGenerator(
        include_deprecated=include_deprecated,
        filter_tags=filter_tags,
    )
    result = generator.generate(schema)

    # Add parser warnings
    result.warnings.extend(parser.warnings)
    result.errors.extend(parser.errors)

    logger.info(
        "API conversion completed",
        tools=len(result.server.tools),
        resources=len(result.server.resources),
        warnings=len(result.warnings),
    )

    return result


def _load_source(
    source: str | Path | dict[str, Any],
    allowed_domains: set[str] | None = None,
) -> dict[str, Any]:
    """Loads source."""
    # Return directly if dict
    if isinstance(source, dict):
        return source

    source_str = str(source)

    # Download if URL
    if source_str.startswith(("http://", "https://")):
        return fetch_spec_sync(source_str, allowed_domains)

    # File path
    path = Path(source_str)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")

    import json

    import yaml

    content = path.read_text(encoding="utf-8")

    if path.suffix in (".yaml", ".yml"):
        return yaml.safe_load(content)
    elif path.suffix == ".json":
        return json.loads(content)
    else:
        # Try JSON first
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            return yaml.safe_load(content)


def _get_parser(spec: dict[str, Any], format: str | None = None) -> BaseParser:
    """Returns appropriate parser."""
    if format:
        format_lower = format.lower()
        if format_lower in ("openapi", "swagger"):
            return OpenAPIParser()
        elif format_lower == "postman":
            return PostmanParser()
        elif format_lower == "manual":
            return ManualParser()
        else:
            raise ValueError(f"Unknown format: {format}")

    # Auto-detect
    parser = detect_format(spec)
    if not parser:
        raise ValueError(
            "Could not detect specification format. "
            "Supported formats: OpenAPI 3.x, Postman Collection, Manual"
        )

    return parser


def parse_spec(
    source: str | Path | dict[str, Any],
    format: str | None = None,
    allowed_domains: set[str] | None = None,
) -> UnifiedAPISchema:
    """Parses API specification (without MCP conversion).

    Args:
        source: API definition source
        format: Force format
        allowed_domains: Allowed domains

    Returns:
        UnifiedAPISchema
    """
    spec = _load_source(source, allowed_domains)
    parser = _get_parser(spec, format)
    return parser.parse(spec)
