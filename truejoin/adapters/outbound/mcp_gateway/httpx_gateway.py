"""Adapter: ApiGateway implemented with httpx."""

from collections.abc import Mapping
from typing import Any

import httpx
import structlog

from truejoin.adapters.outbound.mcp_gateway.redirect_guard import send_following_redirects
from truejoin.domain.mcp.url_policy import mask_sensitive_data
from truejoin.ports.api_gateway import ApiResponse

logger = structlog.get_logger()

# Default timeout values
DEFAULT_TIMEOUT = 30.0
DEFAULT_CONNECT_TIMEOUT = 10.0


def bearer_header(auth_token: str) -> str:
    """Normalizes a raw token into an Authorization header value."""
    if auth_token.lower().startswith("bearer "):
        return auth_token
    return f"Bearer {auth_token}"


class HttpxApiGateway:
    """Async HTTP transport that applies the URL policy to every redirect hop.

    `application.invoke` checks the URL it is about to ask for, which is what
    guarantees nothing is sent for a host the caller may not reach. That check
    cannot see where a redirect points, so the policy is applied again here,
    per hop — see `redirect_guard`.

    A gateway built without `allowed_domains` still enforces the private-IP
    blocklist on every hop; it just has no allowlist to apply.
    """

    def __init__(
        self,
        timeout: float = DEFAULT_TIMEOUT,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        headers: Mapping[str, str] | None = None,
        auth_token: str | None = None,
        auth_header: str = "Authorization",
        allowed_domains: set[str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        """Creates the gateway.

        Args:
            timeout: Request timeout (seconds)
            connect_timeout: Connection timeout (seconds)
            headers: Default headers sent with every request
            auth_token: Optional API token; sent as a Bearer header
            auth_header: Header name used for auth_token
            allowed_domains: Hosts tool calls may reach, checked on every
                redirect hop. None means any public host.
            transport: Replaces the real network transport. For tests — httpx
                builds its own when this is None, so production is unaffected.
        """
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.headers: dict[str, str] = dict(headers or {})
        self.auth_header = auth_header
        if auth_token:
            self.headers[auth_header] = bearer_header(auth_token)
        self.allowed_domains = allowed_domains
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "HttpxApiGateway":
        """Context manager entry."""
        self._get_client()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        await self.aclose()

    def _get_client(self) -> httpx.AsyncClient:
        """Returns the (lazily created) HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=self.connect_timeout),
                headers=self.headers,
                # Followed by hand in `redirect_guard`, so the policy applies to
                # each hop. Leaving this True is what made the check advisory.
                follow_redirects=False,
                transport=self._transport,
            )
        return self._client

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> ApiResponse:
        """Sends an HTTP request and returns a transport-agnostic response."""
        client = self._get_client()

        logger.info(
            "Sending HTTP request",
            method=method,
            url=url,
            params=mask_sensitive_data(dict(params or {})),
        )

        # Built rather than sent directly, so the client's default headers
        # (auth among them) are merged before the guard sees the request.
        request = client.build_request(
            method=method,
            url=url,
            params=params,
            json=json,
            headers=headers,
        )
        response = await send_following_redirects(
            client,
            request,
            allowed_domains=self.allowed_domains,
            auth_header=self.auth_header,
        )

        final_url = str(response.url)
        logger.info(
            "HTTP response received",
            status_code=response.status_code,
            url=url,
            final_url=final_url,
        )

        return ApiResponse(
            status_code=response.status_code,
            headers=dict(response.headers),
            text=response.text,
            final_url=final_url,
        )

    async def aclose(self) -> None:
        """Closes the underlying client."""
        if self._client:
            await self._client.aclose()
            self._client = None
