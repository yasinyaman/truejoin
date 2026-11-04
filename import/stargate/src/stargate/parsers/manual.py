"""Deprecated import path. Use stargate.adapters.parsers.manual instead."""

import warnings

from stargate.adapters.parsers.manual import (
    ManualAPIConfig,
    ManualEndpointConfig,
    ManualParser,
    create_manual_api,
)

warnings.warn(
    "stargate.parsers.manual is deprecated; use stargate.adapters.parsers.manual",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    "ManualParser",
    "ManualAPIConfig",
    "ManualEndpointConfig",
    "create_manual_api",
]
