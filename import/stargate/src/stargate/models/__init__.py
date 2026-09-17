"""Deprecated import path. Use stargate.domain.api / stargate.domain.mcp instead."""

import warnings

from stargate.domain.api import (
    Parameter,
    RequestBody,
    Response,
    SecurityRequirement,
    SecurityScheme,
    UnifiedAPISchema,
    UnifiedEndpoint,
)
from stargate.domain.mcp import (
    MCPResource,
    MCPServer,
    MCPTool,
)

warnings.warn(
    "stargate.models is deprecated; use stargate.domain.api / stargate.domain.mcp",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    "UnifiedAPISchema",
    "UnifiedEndpoint",
    "Parameter",
    "RequestBody",
    "Response",
    "SecurityRequirement",
    "SecurityScheme",
    "MCPServer",
    "MCPTool",
    "MCPResource",
]
