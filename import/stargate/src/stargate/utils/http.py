"""HTTP client helpers."""

import asyncio
from typing import Any

import httpx
import structlog

from stargate.domain.url_policy import mask_sensitive_data, validate_url

logger = structlog.get_logger()

# Default timeout values
DEFAULT_TIMEOUT = 30.0
DEFAULT_CONNECT_TIMEOUT = 10.0


class HTTPClient:
    """Async HTTP client."""

    def __init__(
        self,
        base_url: str = "",
        timeout: float = DEFAULT_TIMEOUT,
        headers: dict[str, str] | None = None,
        allowed_domains: set[str] | None = None,
    ):
        """Creates HTTPClient.

        Args:
            base_url: API base URL
            timeout: Request timeout (seconds)
            headers: Default headers
            allowed_domains: Allowed domains (SSRF protection)
        """
        self.base_url = base_url.rstrip("/") if base_url else ""
        self.timeout = timeout
        self.headers = headers or {}
        self.allowed_domains = allowed_domains
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "HTTPClient":
        """Context manager entry."""
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout, connect=DEFAULT_CONNECT_TIMEOUT),
            headers=self.headers,
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        if self._client:
            await self._client.aclose()
            self._client = None

    def _get_client(self) -> httpx.AsyncClient:
        """Returns HTTP client."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self.timeout, connect=DEFAULT_CONNECT_TIMEOUT),
                headers=self.headers,
                follow_redirects=True,
            )
        return self._client

    def _build_url(self, path: str) -> str:
        """Builds full URL."""
        if path.startswith(("http://", "https://")):
            return path
        return f"{self.base_url}/{path.lstrip('/')}"

    async def request(
        self,
        method: str,
        path: str,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        data: Any = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Sends HTTP request.

        Args:
            method: HTTP method
            path: Request path
            params: Query parameters
            json: JSON body
            data: Form data
            headers: Additional headers

        Returns:
            HTTP response

        Raises:
            ValueError: Unsafe URL
            httpx.HTTPError: HTTP error
        """
        url = self._build_url(path)

        # URL security check
        is_valid, error = validate_url(url, self.allowed_domains)
        if not is_valid:
            raise ValueError(f"Unsafe URL: {error}")

        client = self._get_client()

        logger.info(
            "Sending HTTP request",
            method=method,
            url=url,
            params=mask_sensitive_data(params or {}),
        )

        response = await client.request(
            method=method,
            url=url,
            params=params,
            json=json,
            data=data,
            headers=headers,
        )

        logger.info(
            "HTTP response received",
            status_code=response.status_code,
            url=url,
        )

        return response

    async def get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Sends GET request."""
        return await self.request("GET", path, params=params, headers=headers)

    async def post(
        self,
        path: str,
        json: dict[str, Any] | None = None,
        data: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Sends POST request."""
        return await self.request(
            "POST", path, params=params, json=json, data=data, headers=headers
        )

    async def put(
        self,
        path: str,
        json: dict[str, Any] | None = None,
        data: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Sends PUT request."""
        return await self.request("PUT", path, params=params, json=json, data=data, headers=headers)

    async def patch(
        self,
        path: str,
        json: dict[str, Any] | None = None,
        data: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Sends PATCH request."""
        return await self.request(
            "PATCH", path, params=params, json=json, data=data, headers=headers
        )

    async def delete(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        """Sends DELETE request."""
        return await self.request("DELETE", path, params=params, headers=headers)

    async def close(self):
        """Closes client."""
        if self._client:
            await self._client.aclose()
            self._client = None


async def fetch_spec(url: str, allowed_domains: set[str] | None = None) -> dict[str, Any]:
    """Downloads API specification from URL.

    Args:
        url: Specification URL
        allowed_domains: Allowed domains

    Returns:
        Parsed specification

    Raises:
        ValueError: Unsafe URL or invalid response
    """
    async with HTTPClient(allowed_domains=allowed_domains) as client:
        response = await client.get(url)
        response.raise_for_status()

        content_type = response.headers.get("content-type", "")

        if "yaml" in content_type or url.endswith((".yaml", ".yml")):
            import yaml

            return yaml.safe_load(response.text)
        else:
            return response.json()


def fetch_spec_sync(url: str, allowed_domains: set[str] | None = None) -> dict[str, Any]:
    """Downloads API specification from URL synchronously."""
    return asyncio.run(fetch_spec(url, allowed_domains))
