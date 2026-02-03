"""MCP Resources generator."""

from typing import Any

import structlog

from stargate.models.mcp import MCPResource
from stargate.models.unified import HTTPMethod, UnifiedAPISchema, UnifiedEndpoint

logger = structlog.get_logger()


class ResourcesGenerator:
    """Creates MCP resources from API endpoints and schemas."""

    def __init__(
        self,
        include_schemas: bool = True,
        include_get_endpoints: bool = True,
        filter_tags: list[str] | None = None,
    ):
        """Creates ResourcesGenerator.

        Args:
            include_schemas: Include schema definitions as resources
            include_get_endpoints: Include GET endpoints as resources
            filter_tags: Only include endpoints with these tags
        """
        self.include_schemas = include_schemas
        self.include_get_endpoints = include_get_endpoints
        self.filter_tags = filter_tags
        self.warnings: list[str] = []

    def generate(self, schema: UnifiedAPISchema) -> list[MCPResource]:
        """Creates MCP resources from schema.

        Args:
            schema: Unified API schema

        Returns:
            List of MCP resources
        """
        self.warnings = []
        resources = []
        base_url = schema.get_base_url()

        # Create resources from GET endpoints
        if self.include_get_endpoints:
            for endpoint in schema.endpoints:
                if endpoint.method != HTTPMethod.GET:
                    continue

                # Tag filter
                if self.filter_tags:
                    if not any(tag in endpoint.tags for tag in self.filter_tags):
                        continue

                try:
                    resource = self._create_endpoint_resource(endpoint, base_url)
                    resources.append(resource)
                except Exception as e:
                    self.warnings.append(f"Resource creation error ({endpoint.operation_id}): {e}")

        # Create resources from schemas
        if self.include_schemas:
            for schema_name, schema_def in schema.schemas.items():
                try:
                    resource = self._create_schema_resource(schema_name, schema_def)
                    resources.append(resource)
                except Exception as e:
                    self.warnings.append(f"Schema resource creation error ({schema_name}): {e}")

        logger.info(f"{len(resources)} MCP resources created")
        return resources

    def _create_endpoint_resource(
        self,
        endpoint: UnifiedEndpoint,
        base_url: str,
    ) -> MCPResource:
        """Creates resource from GET endpoint."""
        # Resource name
        name = self._generate_resource_name(endpoint)

        # Description
        description = endpoint.summary or endpoint.description or f"GET {endpoint.path}"

        # URI
        uri = self._generate_uri(endpoint)

        return MCPResource(
            name=name,
            description=description,
            uri=uri,
            mime_type="application/json",
            http_path=endpoint.path,
            base_url=base_url,
        )

    def _create_schema_resource(
        self,
        schema_name: str,
        schema_def: dict[str, Any],
    ) -> MCPResource:
        """Creates resource from schema definition."""
        # Description
        description = schema_def.get(
            "description",
            f"Schema definition for {schema_name}",
        )

        return MCPResource(
            name=f"{schema_name}Schema",
            description=description,
            uri=f"schema://{schema_name.lower()}",
            mime_type="application/json",
            schema_def=schema_def,
        )

    def _generate_resource_name(self, endpoint: UnifiedEndpoint) -> str:
        """Generates resource name."""
        # Derive from operation ID
        name = endpoint.operation_id

        # Clean invalid characters
        name = "".join(c if c.isalnum() or c == "_" else "_" for c in name)

        return name or "unnamed_resource"

    def _generate_uri(self, endpoint: UnifiedEndpoint) -> str:
        """Generates resource URI."""
        # Convert path to URI format
        path = endpoint.path.strip("/")

        # Keep path parameters
        # /users/{id} -> api://users/{id}
        return f"api://{path}"


def generate_resources(
    schema: UnifiedAPISchema,
    include_schemas: bool = True,
    include_get_endpoints: bool = True,
    filter_tags: list[str] | None = None,
) -> list[MCPResource]:
    """Creates MCP resources from schema (helper function).

    Args:
        schema: Unified API schema
        include_schemas: Include schema definitions
        include_get_endpoints: Include GET endpoints
        filter_tags: Only include endpoints with these tags

    Returns:
        List of MCP resources
    """
    generator = ResourcesGenerator(
        include_schemas=include_schemas,
        include_get_endpoints=include_get_endpoints,
        filter_tags=filter_tags,
    )
    return generator.generate(schema)
