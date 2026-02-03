"""MCP Tools generator."""

from typing import Any

import structlog

from stargate.models.mcp import MCPTool
from stargate.models.unified import (
    HTTPMethod,
    UnifiedAPISchema,
    UnifiedEndpoint,
)
from stargate.utils.schema import schema_to_mcp_input

logger = structlog.get_logger()


class ToolsGenerator:
    """Creates MCP tools from API endpoints."""

    def __init__(
        self,
        include_deprecated: bool = False,
        filter_tags: list[str] | None = None,
        filter_methods: list[HTTPMethod] | None = None,
    ):
        """Creates ToolsGenerator.

        Args:
            include_deprecated: Include deprecated endpoints
            filter_tags: Only include endpoints with these tags
            filter_methods: Only include these HTTP methods
        """
        self.include_deprecated = include_deprecated
        self.filter_tags = filter_tags
        self.filter_methods = filter_methods
        self.warnings: list[str] = []

    def generate(self, schema: UnifiedAPISchema) -> list[MCPTool]:
        """Creates MCP tools from schema.

        Args:
            schema: Unified API schema

        Returns:
            List of MCP tools
        """
        self.warnings = []
        tools = []
        base_url = schema.get_base_url()

        for endpoint in schema.endpoints:
            # Filtering
            if not self._should_include(endpoint):
                continue

            try:
                tool = self._create_tool(endpoint, schema, base_url)
                tools.append(tool)
            except Exception as e:
                self.warnings.append(f"Tool creation error ({endpoint.operation_id}): {e}")
                logger.warning(
                    "Tool creation error",
                    operation_id=endpoint.operation_id,
                    error=str(e),
                )

        logger.info(f"{len(tools)} MCP tools created")
        return tools

    def _should_include(self, endpoint: UnifiedEndpoint) -> bool:
        """Checks if endpoint should be included."""
        # Deprecated check
        if endpoint.deprecated and not self.include_deprecated:
            return False

        # Tag filter
        if self.filter_tags:
            if not any(tag in endpoint.tags for tag in self.filter_tags):
                return False

        # Method filter
        if self.filter_methods:
            if endpoint.method not in self.filter_methods:
                return False

        return True

    def _create_tool(
        self,
        endpoint: UnifiedEndpoint,
        schema: UnifiedAPISchema,
        base_url: str,
    ) -> MCPTool:
        """Creates MCP tool from endpoint."""
        # Tool name
        name = self._generate_tool_name(endpoint)

        # Description
        description = self._generate_description(endpoint)

        # Input schema
        input_schema = self._generate_input_schema(endpoint, schema.schemas)

        # Security info
        security = [{"scheme": sec.scheme_name, "scopes": sec.scopes} for sec in endpoint.security]

        return MCPTool(
            name=name,
            description=description,
            input_schema=input_schema,
            http_method=endpoint.method.value,
            http_path=endpoint.path,
            base_url=base_url,
            security=security,
        )

    def _generate_tool_name(self, endpoint: UnifiedEndpoint) -> str:
        """Generates tool name."""
        # Use operation ID
        name = endpoint.operation_id

        # Clean invalid characters
        name = "".join(c if c.isalnum() or c == "_" else "_" for c in name)

        # Add prefix if starts with digit
        if name and name[0].isdigit():
            name = f"op_{name}"

        return name or "unnamed_operation"

    def _generate_description(self, endpoint: UnifiedEndpoint) -> str:
        """Generates tool description."""
        parts = []

        # Summary or description
        if endpoint.summary:
            parts.append(endpoint.summary)
        elif endpoint.description:
            # Get first sentence
            first_sentence = endpoint.description.split(".")[0]
            parts.append(first_sentence)

        # HTTP info
        parts.append(f"[{endpoint.method.value} {endpoint.path}]")

        # Tags
        if endpoint.tags:
            parts.append(f"Tags: {', '.join(endpoint.tags)}")

        return " | ".join(parts)

    def _generate_input_schema(
        self,
        endpoint: UnifiedEndpoint,
        definitions: dict[str, Any],
    ) -> dict[str, Any]:
        """Generates input schema."""
        # Convert parameters to dict format
        params = []
        for param in endpoint.parameters:
            params.append(
                {
                    "name": param.name,
                    "in": param.location.value,
                    "description": param.description,
                    "required": param.required,
                    "schema": param.schema_,
                }
            )

        # Request body schema
        body_schema = None
        if endpoint.request_body:
            # Prefer application/json
            for media_type in ("application/json", "application/*", "*/*"):
                if media_type in endpoint.request_body.content:
                    body_schema = endpoint.request_body.content[media_type].schema_
                    break

            # Any content type
            if body_schema is None and endpoint.request_body.content:
                first_content = next(iter(endpoint.request_body.content.values()))
                body_schema = first_content.schema_

        return schema_to_mcp_input(params, body_schema, definitions)


def generate_tools(
    schema: UnifiedAPISchema,
    include_deprecated: bool = False,
    filter_tags: list[str] | None = None,
) -> list[MCPTool]:
    """Creates MCP tools from schema (helper function).

    Args:
        schema: Unified API schema
        include_deprecated: Include deprecated endpoints
        filter_tags: Only include endpoints with these tags

    Returns:
        List of MCP tools
    """
    generator = ToolsGenerator(
        include_deprecated=include_deprecated,
        filter_tags=filter_tags,
    )
    return generator.generate(schema)
