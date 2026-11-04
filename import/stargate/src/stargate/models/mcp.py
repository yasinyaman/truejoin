"""Deprecated import path. Use stargate.domain.mcp instead."""

import warnings

from stargate.domain.mcp import (
    MCPConversionResult,
    MCPResource,
    MCPServer,
    MCPTool,
)

warnings.warn(
    "stargate.models.mcp is deprecated; use stargate.domain.mcp",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    "MCPTool",
    "MCPResource",
    "MCPServer",
    "MCPConversionResult",
]
