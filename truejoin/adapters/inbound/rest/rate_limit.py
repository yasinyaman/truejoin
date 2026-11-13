"""Rate limiting keyed per API key (falls back to client IP)."""

from __future__ import annotations

import hashlib
from typing import Any

from slowapi.util import get_remote_address


def rate_limit_key(request: Any, header: str = "X-API-Key") -> str:
    """Bucket on a hash of the API key header when present, else the remote address.

    Keying on the API key resists ``X-Forwarded-For`` spoofing and avoids
    throttling many clients that share one NAT/proxy IP. The key is hashed
    so the raw secret is never used as an in-memory bucket name.
    """
    api_key = request.headers.get(header)
    if api_key:
        return "key:" + hashlib.sha256(api_key.encode()).hexdigest()[:16]
    return str(get_remote_address(request))
