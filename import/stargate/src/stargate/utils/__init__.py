"""Deprecated import path. Use stargate.domain.schema, stargate.domain.url_policy or adapters."""

import warnings

from stargate.adapters.httpx_gateway import HttpxApiGateway
from stargate.domain.schema import flatten_schema, resolve_refs
from stargate.domain.url_policy import is_safe_domain, validate_url

HTTPClient = HttpxApiGateway

warnings.warn(
    "stargate.utils is deprecated; use stargate.domain.schema / stargate.domain.url_policy / "
    "stargate.adapters.httpx_gateway",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    "HTTPClient",
    "resolve_refs",
    "flatten_schema",
    "validate_url",
    "is_safe_domain",
]
