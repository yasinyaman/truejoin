"""Deprecated import path. Use stargate.adapters.parsers.base instead."""

import warnings

from stargate.adapters.parsers.base import BaseParser

warnings.warn(
    "stargate.parsers.base is deprecated; use stargate.adapters.parsers.base",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    "BaseParser",
]
