"""MCP Tools generator."""

from typing import Any

import structlog

from stargate.domain.api import (
    HTTPMethod,
    UnifiedAPISchema,
    UnifiedEndpoint,
)
from stargate.domain.mcp import MCPTool
from stargate.domain.schema import schema_to_mcp_input

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

        # x-llm-context from endpoint extensions
        x_llm_context = endpoint.extensions.get("x-llm-context")

        # Description (enriched with x-llm-context if available)
        description = self._generate_description(endpoint, x_llm_context)

        # Input schema
        input_schema = self._generate_input_schema(endpoint, schema.schemas)

        # Enrich input schema properties with x-llm-context column info
        if x_llm_context:
            input_schema = self._enrich_input_schema_with_llm_context(
                input_schema, x_llm_context
            )

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
            x_llm_context=x_llm_context,
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

    def _generate_description(
        self,
        endpoint: UnifiedEndpoint,
        x_llm_context: dict[str, Any] | None = None,
    ) -> str:
        """Generates tool description enriched with x-llm-context metadata."""
        parts = []

        # Summary or human_name from x-llm-context
        if endpoint.summary:
            parts.append(endpoint.summary)
        elif x_llm_context and x_llm_context.get("human_name"):
            parts.append(x_llm_context["human_name"])
        elif endpoint.description:
            # Get first sentence
            first_sentence = endpoint.description.split(".")[0]
            parts.append(first_sentence)

        # HTTP info
        parts.append(f"[{endpoint.method.value} {endpoint.path}]")

        # Tags from x-llm-context or endpoint
        if x_llm_context and x_llm_context.get("tags"):
            parts.append(f"Tags: {', '.join(x_llm_context['tags'])}")
        elif endpoint.tags:
            parts.append(f"Tags: {', '.join(endpoint.tags)}")

        # Table description from x-llm-context
        if x_llm_context and x_llm_context.get("description"):
            parts.append(x_llm_context["description"])

        # Relationships from x-llm-context
        if x_llm_context and x_llm_context.get("relationships"):
            rel_parts = []
            for rel in x_llm_context["relationships"]:
                if isinstance(rel, dict):
                    rel_str = rel.get("description", "")
                    if not rel_str:
                        from_col = rel.get("from", "")
                        to_table = rel.get("to_table", "")
                        to_col = rel.get("to_column", "")
                        rel_type = rel.get("type", "")
                        rel_str = f"{from_col} → {to_table}.{to_col} ({rel_type})"
                    rel_parts.append(rel_str)
                elif isinstance(rel, str):
                    rel_parts.append(rel)
            if rel_parts:
                parts.append(f"Relationships: {'; '.join(rel_parts)}")

        # Column summary from x-llm-context
        if x_llm_context and x_llm_context.get("columns"):
            col_names = [
                col["name"] if isinstance(col, dict) else str(col)
                for col in x_llm_context["columns"]
            ]
            if col_names:
                parts.append(f"Columns: {', '.join(col_names)}")

        # Row count hint
        if x_llm_context and x_llm_context.get("row_count") is not None:
            parts.append(f"~{x_llm_context['row_count']} rows")

        return " | ".join(parts)

    def _enrich_input_schema_with_llm_context(
        self,
        input_schema: dict[str, Any],
        x_llm_context: dict[str, Any],
    ) -> dict[str, Any]:
        """Enriches input schema properties with x-llm-context column metadata.

        Adds descriptions, examples, semantic types and tags from
        x-llm-context columns to matching input schema properties.
        """
        columns = x_llm_context.get("columns", [])
        if not columns:
            return input_schema

        # Build column lookup by name
        col_lookup: dict[str, dict[str, Any]] = {}
        for col in columns:
            if isinstance(col, dict) and "name" in col:
                col_lookup[col["name"]] = col

        properties = input_schema.get("properties", {})
        for prop_name, prop_schema in properties.items():
            col_info = col_lookup.get(prop_name)
            if not col_info:
                continue

            # Add description from column if not already set or is generic
            col_desc = col_info.get("description", "")
            existing_desc = prop_schema.get("description", "")
            if col_desc and (
                not existing_desc
                or existing_desc in ("[path]", "[query]", "[header]")
            ):
                # Preserve location prefix if present
                if existing_desc in ("[path]", "[query]", "[header]"):
                    prop_schema["description"] = f"{existing_desc} {col_desc}"
                else:
                    prop_schema["description"] = col_desc

            # Add examples if available
            examples = col_info.get("examples")
            if examples and "examples" not in prop_schema:
                prop_schema["examples"] = examples[:5]  # Limit to 5 examples

            # Add semantic_type as x-semantic-type
            semantic_type = col_info.get("semantic_type")
            if semantic_type:
                prop_schema["x-semantic-type"] = semantic_type

            # Add foreign key info
            fk = col_info.get("foreign_key")
            if fk:
                prop_schema["x-foreign-key"] = fk

            # Add tags
            col_tags = col_info.get("tags")
            if col_tags:
                prop_schema["x-tags"] = col_tags

        return input_schema

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
