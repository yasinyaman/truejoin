"""Deprecated import path. Use stargate.application.generators instead."""

import warnings

from stargate.application.generators import (
    MCPServerGenerator,
    ResourcesGenerator,
    ToolsGenerator,
)

warnings.warn(
    "stargate.generators is deprecated; use stargate.application.generators",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    "MCPServerGenerator",
    "ToolsGenerator",
    "ResourcesGenerator",
]
