"""Composition root #2: a runnable MCP server.

MCPServerRuntime wires ToolInvoker/ResourceReader (application) to an
ApiGateway (default: httpx) and an MCPHost (default: the MCP SDK server). Both can be
injected for tests or alternative transports.
"""

from collections.abc import Callable
from typing import Any

import structlog

from stargate.adapters.httpx_gateway import HttpxApiGateway
from stargate.adapters.mcp_sdk_host import MCPSdkHost
from stargate.adapters.mcp_wire import resource_to_mcp, tool_to_mcp
from stargate.application.invoke import ResourceReader, ToolInvoker
from stargate.domain.mcp import MCPResource, MCPServer, MCPTool
from stargate.ports.api_gateway import ApiGateway
from stargate.ports.mcp_host import MCPHost, ResourceHandler, ToolHandler

logger = structlog.get_logger()


class MCPServerRuntime:
    """Runs an MCP server definition as an actual MCP server."""

    def __init__(
        self,
        server: MCPServer,
        auth_token: str | None = None,
        auth_header: str = "Authorization",
        timeout: float = 30.0,
        allowed_domains: set[str] | None = None,
        gateway: ApiGateway | None = None,
        host_factory: Callable[[str], MCPHost] = MCPSdkHost,
    ):
        """Creates MCPServerRuntime.

        Args:
            server: MCP Server definition
            auth_token: API authentication token (sent as Bearer)
            auth_header: Auth header name
            timeout: HTTP request timeout
            allowed_domains: Domain allowlist for outgoing requests (SSRF protection)
            gateway: HTTP transport; defaults to HttpxApiGateway
            host_factory: MCP framework binding; defaults to MCPSdkHost
        """
        self.server = server
        self.auth_token = auth_token
        self.auth_header = auth_header
        self.timeout = timeout
        self.allowed_domains = allowed_domains
        # Given to the gateway as well as the use cases: the use cases check
        # the URL they are about to ask for, the gateway checks every redirect
        # they cannot see. An injected gateway is the caller's to configure.
        self._gateway: ApiGateway = gateway or HttpxApiGateway(
            timeout=timeout,
            auth_token=auth_token,
            auth_header=auth_header,
            allowed_domains=allowed_domains,
        )
        self._host_factory = host_factory
        self._invoker = ToolInvoker(server, self._gateway, allowed_domains)
        self._reader = ResourceReader(server, self._gateway, allowed_domains)

    async def __aenter__(self) -> "MCPServerRuntime":
        """Context manager entry (connections are opened lazily)."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit: releases HTTP connections."""
        await self._gateway.aclose()

    def get_tools(self) -> list[dict[str, Any]]:
        """Returns tool list in MCP format."""
        return [tool_to_mcp(tool) for tool in self.server.tools]

    def get_resources(self) -> list[dict[str, Any]]:
        """Returns resource list in MCP format."""
        return [resource_to_mcp(resource) for resource in self.server.resources]

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Calls an MCP tool (HTTP request against the API)."""
        return await self._invoker.invoke(tool_name, arguments)

    async def read_resource(self, uri: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Reads an MCP resource; `params` fill URI template placeholders."""
        return await self._reader.read(uri, params)

    def run(self, transport: str = "stdio"):
        """Registers everything on the host and serves.

        Args:
            transport: Transport type (stdio, sse, streamable-http)
        """
        host = self._host_factory(self.server.name)

        for tool in self.server.tools:
            host.register_tool(tool, self._tool_handler(tool))
        for resource in self.server.resources:
            host.register_resource(resource, self._resource_handler(resource))

        logger.info(
            "Starting MCP server",
            name=self.server.name,
            transport=transport,
            tool_count=len(self.server.tools),
            resource_count=len(self.server.resources),
        )
        host.run(transport=transport)

    def _tool_handler(self, tool: MCPTool) -> ToolHandler:
        async def handle(arguments: dict[str, Any]) -> Any:
            async with self:
                return await self.call_tool(tool.name, arguments)

        return handle

    def _resource_handler(self, resource: MCPResource) -> ResourceHandler:
        async def handle(params: dict[str, str]) -> str:
            async with self:
                result = await self.read_resource(resource.uri, params)
                return result.get("content", "")

        return handle
