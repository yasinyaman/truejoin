"""Deprecated import path. Use stargate.adapters.httpx_gateway / stargate.adapters.spec_loader.

Note: the old ``HTTPClient.request(method, path)`` resolved ``path`` against a
``base_url``; ``HttpxApiGateway.request(method, url)`` expects an absolute URL.
"""

import warnings

from stargate.adapters.httpx_gateway import (
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_TIMEOUT,
    HttpxApiGateway,
)
from stargate.adapters.spec_loader import fetch_spec_sync

HTTPClient = HttpxApiGateway

warnings.warn(
    "stargate.utils.http is deprecated; use stargate.adapters.httpx_gateway",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    "HTTPClient",
    "HttpxApiGateway",
    "fetch_spec_sync",
    "DEFAULT_TIMEOUT",
    "DEFAULT_CONNECT_TIMEOUT",
]
