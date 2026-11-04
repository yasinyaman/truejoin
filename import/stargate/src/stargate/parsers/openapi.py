"""Deprecated import path. Use stargate.adapters.parsers.openapi instead."""

import warnings

from stargate.adapters.parsers.openapi import OpenAPIParser

warnings.warn(
    "stargate.parsers.openapi is deprecated; use stargate.adapters.parsers.openapi",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    "OpenAPIParser",
]
