"""Adapters: SpecLoader implementations (in-memory dict, local file, remote URL)."""

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
import structlog
import yaml

from truejoin.adapters.outbound.mcp_gateway.redirect_guard import send_following_redirects_sync
from truejoin.domain.mcp.url_policy import validate_url
from truejoin.ports.spec_loader import SpecLoader, SpecSource

logger = structlog.get_logger()

DEFAULT_TIMEOUT = 30.0
DEFAULT_CONNECT_TIMEOUT = 10.0


def _ensure_mapping(data: Any) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise ValueError(f"Invalid specification format: {type(data).__name__}")
    return data


def _is_url(source: SpecSource) -> bool:
    return isinstance(source, str | Path) and str(source).startswith(("http://", "https://"))


class DictSpecLoader:
    """Accepts an already-parsed specification."""

    def supports(self, source: SpecSource) -> bool:
        return isinstance(source, dict)

    def load(self, source: SpecSource, allowed_domains: set[str] | None = None) -> dict[str, Any]:
        return _ensure_mapping(source)


class FileSpecLoader:
    """Reads a JSON or YAML file from the local filesystem."""

    def supports(self, source: SpecSource) -> bool:
        return isinstance(source, str | Path) and not _is_url(source)

    def load(self, source: SpecSource, allowed_domains: set[str] | None = None) -> dict[str, Any]:
        path = Path(str(source))
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")

        logger.info("Loading specification file", path=str(path))
        content = path.read_text(encoding="utf-8")

        if path.suffix in (".yaml", ".yml"):
            data = yaml.safe_load(content)
        elif path.suffix == ".json":
            data = json.loads(content)
        else:
            # Unknown extension: try JSON first, then YAML
            try:
                data = json.loads(content)
            except json.JSONDecodeError:
                data = yaml.safe_load(content)

        return _ensure_mapping(data)


class UrlSpecLoader:
    """Downloads a JSON or YAML document over HTTP(S) with SSRF checks."""

    def __init__(
        self,
        timeout: float = DEFAULT_TIMEOUT,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
        transport: httpx.BaseTransport | None = None,
    ):
        """
        Args:
            transport: Replaces the real network transport. For tests — httpx
                builds its own when this is None.
        """
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self._transport = transport

    def supports(self, source: SpecSource) -> bool:
        return _is_url(source)

    def load(self, source: SpecSource, allowed_domains: set[str] | None = None) -> dict[str, Any]:
        url = str(source)

        is_valid, error = validate_url(url, allowed_domains)
        if not is_valid:
            raise ValueError(f"Unsafe URL: {error}")

        logger.info("Downloading specification", url=url)
        with httpx.Client(
            timeout=httpx.Timeout(self.timeout, connect=self.connect_timeout),
            # Followed by hand so the policy applies to each hop, as in the
            # gateway; a spec URL is as redirectable as any other.
            follow_redirects=False,
            transport=self._transport,
        ) as client:
            response = send_following_redirects_sync(
                client,
                client.build_request("GET", url),
                allowed_domains=allowed_domains,
            )
            response.raise_for_status()

        # Sniffed on the URL the body actually came from: a redirect from
        # `spec.json` to a YAML document would otherwise be parsed as JSON.
        final_url = str(response.url)
        content_type = response.headers.get("content-type", "")
        if "yaml" in content_type or final_url.endswith((".yaml", ".yml")):
            data = yaml.safe_load(response.text)
        else:
            data = response.json()

        return _ensure_mapping(data)


class CompositeSpecLoader:
    """Dispatches to the first loader that supports the source."""

    def __init__(self, loaders: Sequence[SpecLoader]):
        self.loaders = list(loaders)

    def supports(self, source: SpecSource) -> bool:
        return any(loader.supports(source) for loader in self.loaders)

    def load(self, source: SpecSource, allowed_domains: set[str] | None = None) -> dict[str, Any]:
        for loader in self.loaders:
            if loader.supports(source):
                return loader.load(source, allowed_domains)
        raise ValueError(f"Unsupported specification source: {type(source).__name__}")


def default_spec_loader() -> SpecLoader:
    """Loader accepting dicts, URLs and file paths (in that order)."""
    return CompositeSpecLoader([DictSpecLoader(), UrlSpecLoader(), FileSpecLoader()])


def fetch_spec_sync(url: str, allowed_domains: set[str] | None = None) -> dict[str, Any]:
    """Downloads an API specification from a URL."""
    return UrlSpecLoader().load(url, allowed_domains)
