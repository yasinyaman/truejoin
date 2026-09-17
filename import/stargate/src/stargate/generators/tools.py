"""Deprecated import path. Use stargate.application.generators.tools instead."""

import warnings

from stargate.application.generators.tools import ToolsGenerator, generate_tools

warnings.warn(
    "stargate.generators.tools is deprecated; use stargate.application.generators.tools",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    "ToolsGenerator",
    "generate_tools",
]
