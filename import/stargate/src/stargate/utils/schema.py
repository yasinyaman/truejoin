"""Deprecated import path. Use stargate.domain.schema instead."""

import warnings

from stargate.domain.schema import (
    flatten_schema,
    resolve_refs,
    schema_to_mcp_input,
)

warnings.warn(
    "stargate.utils.schema is deprecated; use stargate.domain.schema",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    "resolve_refs",
    "flatten_schema",
    "schema_to_mcp_input",
]
