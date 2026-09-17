"""MCP wire-format serialization.

The domain models carry transport details (HTTP method/path, base URL); the MCP
protocol only sees name/description/schema. This module owns that projection.
"""

from typing import Any

from stargate.domain.mcp import MCPResource, MCPServer, MCPTool


def tool_to_mcp(tool: MCPTool) -> dict[str, Any]:
    """Projects a tool onto the MCP protocol shape."""
    result: dict[str, Any] = {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": tool.input_schema,
    }
    if tool.x_llm_context:
        result["x-llm-context"] = tool.x_llm_context
    return result


def resource_to_mcp(resource: MCPResource) -> dict[str, Any]:
    """Projects a resource onto the MCP protocol shape."""
    return {
        "name": resource.name,
        "description": resource.description,
        "uri": resource.uri,
        "mimeType": resource.mime_type,
    }


def server_to_mcp(server: MCPServer) -> dict[str, Any]:
    """Projects a server definition onto the MCP protocol shape."""
    result: dict[str, Any] = {
        "name": server.name,
        "version": server.version,
        "description": server.description,
        "tools": [tool_to_mcp(tool) for tool in server.tools],
        "resources": [resource_to_mcp(resource) for resource in server.resources],
    }
    if server.x_llm_context:
        result["x-llm-context"] = server.x_llm_context
    return result
