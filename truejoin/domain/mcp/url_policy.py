"""URL policy - SSRF protection and URL validation (pure domain rules)."""

import ipaddress
import logging
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# Blocked IP ranges (private networks)
BLOCKED_IP_RANGES = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]


def is_private_ip(ip_str: str) -> bool:
    """Checks if IP address is in private network."""
    try:
        ip = ipaddress.ip_address(ip_str)
        for network in BLOCKED_IP_RANGES:
            if ip in network:
                return True
        return False
    except ValueError:
        return False


def is_safe_domain(domain: str, allowed_domains: set[str] | None = None) -> bool:
    """Checks if domain is safe.

    Args:
        domain: Domain to check
        allowed_domains: Allowed domains (None accepts all domains)

    Returns:
        True if domain is safe
    """
    if not domain:
        return False

    # Localhost check
    if domain.lower() in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        logger.warning("Localhost access blocked: %s", domain)
        return False

    # IP address check
    if is_private_ip(domain):
        logger.warning("Private IP address blocked: %s", domain)
        return False

    # Whitelist check (if specified)
    if allowed_domains is not None:
        if domain.lower() not in {d.lower() for d in allowed_domains}:
            logger.warning(
                "Domain not in whitelist: %s (allowed: %s)", domain, sorted(allowed_domains)
            )
            return False

    return True


def validate_url(
    url: str,
    allowed_domains: set[str] | None = None,
    allowed_schemes: set[str] | None = None,
) -> tuple[bool, str]:
    """Validates if URL is safe.

    Args:
        url: URL to validate
        allowed_domains: Allowed domains (None skips check)
        allowed_schemes: Allowed protocols (default: http, https)

    Returns:
        (is_valid, error_message) tuple
    """
    if allowed_schemes is None:
        allowed_schemes = {"http", "https"}

    try:
        parsed = urlparse(url)
    except Exception as e:
        return False, f"URL parse failed: {e}"

    # Scheme check
    if parsed.scheme.lower() not in allowed_schemes:
        return False, f"Invalid protocol: {parsed.scheme}. Allowed: {allowed_schemes}"

    # Host check
    if not parsed.netloc:
        return False, "No host found in URL"

    # Separate port
    host = parsed.hostname or ""
    if not host:
        return False, "Invalid host"

    # Domain security check
    if not is_safe_domain(host, allowed_domains):
        return False, f"Unsafe domain: {host}"

    return True, ""


def mask_sensitive_data(data: dict, sensitive_keys: set[str] | None = None) -> dict:
    """Masks sensitive data (for logging)."""
    if sensitive_keys is None:
        sensitive_keys = {
            "password",
            "secret",
            "token",
            "api_key",
            "apikey",
            "authorization",
            "auth",
            "key",
            "credential",
        }

    masked = {}
    for key, value in data.items():
        key_lower = key.lower()
        if any(s in key_lower for s in sensitive_keys):
            masked[key] = "***MASKED***"
        elif isinstance(value, dict):
            masked[key] = mask_sensitive_data(value, sensitive_keys)
        else:
            masked[key] = value

    return masked
