"""Port: execute HTTP calls against the target REST API."""

import json as _json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ApiResponse:
    """Transport-agnostic HTTP response."""

    status_code: int
    headers: dict[str, str] = field(default_factory=dict)
    text: str = ""

    @property
    def content_type(self) -> str:
        """Content-Type header value (case-insensitive lookup), or empty string."""
        for key, value in self.headers.items():
            if key.lower() == "content-type":
                return value
        return ""

    def json(self) -> Any:
        """Decodes the body as JSON."""
        return _json.loads(self.text)


class ApiGateway(Protocol):
    """Sends a prepared request and returns the response; owns connection lifecycle."""

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json: Any = None,
        headers: Mapping[str, str] | None = None,
    ) -> ApiResponse:
        """Performs the HTTP request. `url` is absolute."""
        ...

    async def aclose(self) -> None:
        """Releases underlying connections."""
        ...
