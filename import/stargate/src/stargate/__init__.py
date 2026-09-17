"""Stargate - A tool that converts REST APIs into MCP servers."""

from stargate.bootstrap import convert_to_mcp, parse_spec
from stargate.domain.api import UnifiedAPISchema, UnifiedEndpoint
from stargate.domain.mcp import MCPResource, MCPServer, MCPTool

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
