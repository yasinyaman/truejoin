"""Stargate - A tool that converts REST APIs into MCP servers."""

from truejoin.bootstrap import convert_to_mcp, parse_spec
from truejoin.domain.mcp.api import UnifiedAPISchema, UnifiedEndpoint
from truejoin.domain.mcp.mcp import MCPResource, MCPServer, MCPTool

__version__ = "0.1.0"
__all__ = [
    "convert_to_mcp",
    "parse_spec",
    "UnifiedAPISchema",
    "UnifiedEndpoint",
    "MCPServer",
    "MCPTool",
    "MCPResource",
]
