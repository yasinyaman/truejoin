"""Deprecated import path. Use stargate.runtime instead."""

import warnings

from stargate.runtime import MCPServerRuntime

warnings.warn(
    "stargate.server.runtime is deprecated; use stargate.runtime",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["MCPServerRuntime"]
