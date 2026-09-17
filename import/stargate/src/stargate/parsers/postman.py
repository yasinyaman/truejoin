"""Deprecated import path. Use stargate.adapters.parsers.postman instead."""

import warnings

from stargate.adapters.parsers.postman import PostmanParser

warnings.warn(
    "stargate.parsers.postman is deprecated; use stargate.adapters.parsers.postman",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    "PostmanParser",
]
