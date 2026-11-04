"""Deprecated import path. Use stargate.domain.api instead."""

import warnings

from stargate.domain.api import (
    HTTPMethod,
    MediaType,
    Parameter,
    ParameterLocation,
    RequestBody,
    Response,
    SecurityRequirement,
    SecurityScheme,
    SecuritySchemeType,
    ServerInfo,
    UnifiedAPISchema,
    UnifiedEndpoint,
)

warnings.warn(
    "stargate.models.unified is deprecated; use stargate.domain.api",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    "HTTPMethod",
    "ParameterLocation",
    "SecuritySchemeType",
    "Parameter",
    "MediaType",
    "RequestBody",
    "Response",
    "SecurityScheme",
    "SecurityRequirement",
    "UnifiedEndpoint",
    "ServerInfo",
    "UnifiedAPISchema",
]
