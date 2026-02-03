"""MCP (Model Context Protocol) models."""

from typing import Any

from pydantic import BaseModel, Field


class MCPTool(BaseModel):
    """MCP Tool definition.

    Each API endpoint is converted to an MCP tool.
    """

    name: str = Field(..., description="Tool name")
    description: str = Field(default="", description="Tool description")
    input_schema: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}},
        alias="inputSchema",
        description="Input JSON Schema",
    )

    # Additional fields for internal use
    http_method: str = Field(default="GET", description="HTTP method")
    http_path: str = Field(default="/", description="HTTP path")
    base_url: str = Field(default="", description="API base URL")
    security: list[dict[str, Any]] = Field(
        default_factory=list, description="Security requirements"
    )

    model_config = {"populate_by_name": True}

    def to_mcp_format(self) -> dict[str, Any]:
        """Converts to MCP protocol format."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


class MCPResource(BaseModel):
    """MCP Resource definition.

    GET endpoints and schemas are served as MCP resources.
    """

    name: str = Field(..., description="Resource name")
    description: str = Field(default="", description="Resource description")
    uri: str = Field(..., description="Resource URI")
    mime_type: str = Field(
        default="application/json",
        alias="mimeType",
        description="MIME type",
    )

    # Additional fields for internal use
    http_path: str | None = Field(default=None, description="HTTP path (for API resource)")
    base_url: str = Field(default="", description="API base URL")
    schema_def: dict[str, Any] | None = Field(
        default=None, description="Schema definition (for schema resource)"
    )

    model_config = {"populate_by_name": True}

    def to_mcp_format(self) -> dict[str, Any]:
        """Converts to MCP protocol format."""
        return {
            "name": self.name,
            "description": self.description,
            "uri": self.uri,
            "mimeType": self.mime_type,
        }


class MCPServer(BaseModel):
    """MCP Server definition.

    MCP server containing all tools and resources.
    """

    name: str = Field(..., description="Server name")
    version: str = Field(default="1.0.0", description="Server version")
    description: str = Field(default="", description="Server description")
    tools: list[MCPTool] = Field(default_factory=list, description="Tool list")
    resources: list[MCPResource] = Field(default_factory=list, description="Resource list")
    base_url: str = Field(default="", description="API base URL")
    security_schemes: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Security schemes",
    )

    def get_tool(self, name: str) -> MCPTool | None:
        """Returns tool by name."""
        for tool in self.tools:
            if tool.name == name:
                return tool
        return None

    def get_resource(self, uri: str) -> MCPResource | None:
        """Returns resource by URI."""
        for resource in self.resources:
            if resource.uri == uri:
                return resource
        return None

    def to_mcp_format(self) -> dict[str, Any]:
        """Converts to MCP protocol format."""
        return {
            "name": self.name,
            "version": self.version,
            "description": self.description,
            "tools": [tool.to_mcp_format() for tool in self.tools],
            "resources": [resource.to_mcp_format() for resource in self.resources],
        }


class MCPConversionResult(BaseModel):
    """API conversion result."""

    server: MCPServer = Field(..., description="Created MCP server")
    metadata: dict[str, Any] = Field(default_factory=dict, description="API metadata")
    warnings: list[str] = Field(default_factory=list, description="Warnings")
    errors: list[str] = Field(default_factory=list, description="Errors")

    @property
    def success(self) -> bool:
        """Is conversion successful?"""
        return len(self.errors) == 0
