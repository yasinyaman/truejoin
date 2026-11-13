"""MCP (Model Context Protocol) models."""

from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from truejoin.domain.mcp.api import ParameterLocation


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
    parameter_locations: dict[str, ParameterLocation] = Field(
        default_factory=dict,
        description="Where each input property is sent (path/query/header); body props absent",
    )

    # LLM context metadata from OpenAPI x-llm-context extension
    x_llm_context: dict[str, Any] | None = Field(
        default=None,
        description="LLM context metadata (table info, columns, relationships, examples)",
    )

    model_config = {"populate_by_name": True}


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
    x_llm_context: dict[str, Any] | None = Field(
        default=None,
        description="Global LLM context metadata (database info, table summaries)",
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

    def hosts(self) -> set[str]:
        """Every host this definition says it will call.

        The server's base URL plus each tool's and resource's own, since an
        operation may override it. Used as the default domain allowlist when
        serving: a definition that declares where it calls is the best
        statement available of where it *should* be able to call, and it is a
        far safer default than "any public host".
        """
        found = set()
        for url in self.declared_urls():
            host = urlparse(url).hostname
            if host:
                found.add(host.lower())
        return found

    def declared_urls(self) -> list[str]:
        """Every non-empty base URL in this definition, server and per-operation."""
        urls = [self.base_url, *(t.base_url for t in self.tools)]
        urls += [r.base_url for r in self.resources]
        return [url for url in urls if url]

    def unreadable_urls(self) -> list[str]:
        """Declared URLs `hosts()` could take no host from.

        A scheme-less `api.example.com/v1` parses to no hostname, so it
        contributes nothing to the allowlist while the well-formed URLs beside
        it still do. Without this the operation it belongs to is simply
        refused at call time, with a message naming the policy rather than the
        definition that caused it.
        """
        return sorted({url for url in self.declared_urls() if not urlparse(url).hostname})


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
