"""MCP Server generator."""

import structlog

from stargate.generators.resources import ResourcesGenerator
from stargate.generators.tools import ToolsGenerator
from stargate.models.mcp import MCPConversionResult, MCPServer
from stargate.models.unified import UnifiedAPISchema

logger = structlog.get_logger()


class MCPServerGenerator:
    """Creates MCP Server from UnifiedAPISchema."""

    def __init__(
        self,
        include_deprecated: bool = False,
        include_schemas: bool = True,
        include_get_endpoints: bool = True,
        filter_tags: list[str] | None = None,
    ):
        """Creates MCPServerGenerator.

        Args:
            include_deprecated: Include deprecated endpoints
            include_schemas: Include schemas as resources
            include_get_endpoints: Include GET endpoints as resources
            filter_tags: Only include endpoints with these tags
        """
        self.include_deprecated = include_deprecated
        self.include_schemas = include_schemas
        self.include_get_endpoints = include_get_endpoints
        self.filter_tags = filter_tags

    def generate(self, schema: UnifiedAPISchema) -> MCPConversionResult:
        """Creates MCP Server from schema.

        Args:
            schema: Unified API schema

        Returns:
            Conversion result (server, metadata, warnings, errors)
        """
        warnings: list[str] = []
        errors: list[str] = []

        logger.info(
            "Creating MCP Server",
            title=schema.title,
            endpoint_count=len(schema.endpoints),
        )

        # Create tools
        tools_generator = ToolsGenerator(
            include_deprecated=self.include_deprecated,
            filter_tags=self.filter_tags,
        )
        tools = tools_generator.generate(schema)
        warnings.extend(tools_generator.warnings)

        # Create resources
        resources_generator = ResourcesGenerator(
            include_schemas=self.include_schemas,
            include_get_endpoints=self.include_get_endpoints,
            filter_tags=self.filter_tags,
        )
        resources = resources_generator.generate(schema)
        warnings.extend(resources_generator.warnings)

        # Security schemes
        security_schemes = {
            name: scheme.model_dump() for name, scheme in schema.security_schemes.items()
        }

        # Global x-llm-context from OpenAPI extensions
        x_llm_context = schema.extensions.get("x-llm-context")

        # Create server
        server = MCPServer(
            name=schema.title,
            version=schema.version,
            description=schema.description,
            tools=tools,
            resources=resources,
            base_url=schema.get_base_url(),
            security_schemes=security_schemes,
            x_llm_context=x_llm_context,
        )

        # Metadata
        metadata = {
            "title": schema.title,
            "version": schema.version,
            "description": schema.description,
            "servers": [s.model_dump() for s in schema.servers],
            "tags": schema.tags,
            "tool_count": len(tools),
            "resource_count": len(resources),
        }

        logger.info(
            "MCP Server created",
            tool_count=len(tools),
            resource_count=len(resources),
            warning_count=len(warnings),
        )

        return MCPConversionResult(
            server=server,
            metadata=metadata,
            warnings=warnings,
            errors=errors,
        )


def generate_server(
    schema: UnifiedAPISchema,
    include_deprecated: bool = False,
    filter_tags: list[str] | None = None,
) -> MCPConversionResult:
    """Creates MCP Server from schema (helper function).

    Args:
        schema: Unified API schema
        include_deprecated: Include deprecated endpoints
        filter_tags: Only include endpoints with these tags

    Returns:
        Conversion result
    """
    generator = MCPServerGenerator(
        include_deprecated=include_deprecated,
        filter_tags=filter_tags,
    )
    return generator.generate(schema)
