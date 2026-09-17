"""Adapter: ApiGateway implemented with httpx."""

from collections.abc import Mapping
from typing import Any

import httpx
import structlog

from stargate.domain.url_policy import mask_sensitive_data
from stargate.ports.api_gateway import ApiResponse

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
    """Async HTTP transport. URL safety is the caller's responsibility (see application.invoke)."""

    def __init__(
        self,
        timeout: float = DEFAULT_TIMEOUT,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        headers: Mapping[str, str] | None = None,
        auth_token: str | None = None,
        auth_header: str = "Authorization",
    ):
        """Creates the gateway.

        Args:
            timeout: Request timeout (seconds)
            connect_timeout: Connection timeout (seconds)
            headers: Default headers sent with every request
            auth_token: Optional API token; sent as a Bearer header
            auth_header: Header name used for auth_token
        """
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.headers: dict[str, str] = dict(headers or {})
        if auth_token:
            self.headers[auth_header] = bearer_header(auth_token)
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
                follow_redirects=True,
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

        response = await client.request(
            method=method,
            url=url,
            params=params,
            json=json,
            headers=headers,
        )

        logger.info("HTTP response received", status_code=response.status_code, url=url)

        return ApiResponse(
            status_code=response.status_code,
            headers=dict(response.headers),
            text=response.text,
        )

    async def aclose(self) -> None:
        """Closes the underlying client."""
        if self._client:
            await self._client.aclose()
            self._client = None
