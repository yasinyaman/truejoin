"""Stargate - A tool that converts REST APIs into MCP servers."""

from trio.bootstrap import convert_to_mcp, parse_spec
from trio.domain.mcp.api import UnifiedAPISchema, UnifiedEndpoint
from trio.domain.mcp.mcp import MCPResource, MCPServer, MCPTool

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
