"""Deprecated import path. Use stargate (convert_to_mcp, parse_spec) or stargate.bootstrap."""

import warnings

from stargate.bootstrap import PARSERS, convert_to_mcp, detect_format, parse_spec

warnings.warn(
    "stargate.converter is deprecated; use stargate.bootstrap (or `from stargate import ...`)",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    "convert_to_mcp",
    "parse_spec",
    "detect_format",
    "PARSERS",
]
