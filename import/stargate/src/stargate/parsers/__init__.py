"""Deprecated import path. Use stargate.adapters.parsers instead."""

import warnings

from stargate.adapters.parsers import (
    BaseParser,
    ManualParser,
    OpenAPIParser,
    PostmanParser,
)

warnings.warn(
    "stargate.parsers is deprecated; use stargate.adapters.parsers",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    "BaseParser",
    "OpenAPIParser",
    "PostmanParser",
    "ManualParser",
]
