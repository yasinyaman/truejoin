"""Deprecated import path. Use stargate.application.generators.server instead."""

import warnings

from stargate.application.generators.server import MCPServerGenerator, generate_server

warnings.warn(
    "stargate.generators.mcp_server is deprecated; use stargate.application.generators.server",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    "MCPServerGenerator",
    "generate_server",
]
