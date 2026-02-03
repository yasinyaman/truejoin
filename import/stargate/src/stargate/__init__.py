"""Stargate - A tool that converts REST APIs into MCP servers."""

from stargate.converter import convert_to_mcp
from stargate.models.mcp import MCPResource, MCPServer, MCPTool
from stargate.models.unified import UnifiedAPISchema, UnifiedEndpoint

__version__ = "0.1.0"
__all__ = [
    "convert_to_mcp",
    "UnifiedAPISchema",
    "UnifiedEndpoint",
    "MCPServer",
    "MCPTool",
    "MCPResource",
]
