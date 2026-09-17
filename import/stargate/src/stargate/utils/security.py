"""Deprecated import path. Use stargate.domain.url_policy instead."""

import warnings

from stargate.domain.url_policy import (
    BLOCKED_IP_RANGES,
    is_private_ip,
    is_safe_domain,
    mask_sensitive_data,
    validate_url,
)

warnings.warn(
    "stargate.utils.security is deprecated; use stargate.domain.url_policy",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    "BLOCKED_IP_RANGES",
    "is_private_ip",
    "is_safe_domain",
    "validate_url",
    "mask_sensitive_data",
]
