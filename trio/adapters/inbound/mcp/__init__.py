"""MCP (Model Context Protocol) server adapter."""

from trio.adapters.inbound.mcp.server import create_mcp_server

__all__ = ["create_mcp_server"]
