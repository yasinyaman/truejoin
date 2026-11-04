"""SpecLoader adapter tests (no network)."""

from pathlib import Path

import httpx
import pytest

from trio.adapters.outbound.mcp_gateway.redirect_guard import MAX_REDIRECTS
from trio.adapters.outbound.mcp_gateway.spec_loader import (
    CompositeSpecLoader,
    DictSpecLoader,
    FileSpecLoader,
    UrlSpecLoader,
    default_spec_loader,
)

FIXTURES = Path(__file__).parent.parent / "fixtures"


class TestDictSpecLoader:
    def test_supports_only_dicts(self):
        loader = DictSpecLoader()
        assert loader.supports({"openapi": "3.0.0"})
        assert not loader.supports("spec.json")

    def test_returns_document(self):
        spec = {"openapi": "3.0.0"}
        assert DictSpecLoader().load(spec) is spec


class TestFileSpecLoader:
    def test_supports_paths_but_not_urls(self):
        loader = FileSpecLoader()
        assert loader.supports("spec.json")
        assert loader.supports(Path("spec.yaml"))
        assert not loader.supports("https://example.com/spec.json")
        assert not loader.supports({})

    def test_loads_json(self):
        spec = FileSpecLoader().load(FIXTURES / "openapi_petstore.json")
        assert "paths" in spec

    def test_loads_yaml(self, tmp_path):
        path = tmp_path / "spec.yaml"
        path.write_text("openapi: 3.0.0\ninfo:\n  title: T\n")
        assert FileSpecLoader().load(path)["info"]["title"] == "T"

    def test_extensionless_falls_back_to_yaml(self, tmp_path):
        path = tmp_path / "spec"
        path.write_text("openapi: 3.0.0\n")
        assert FileSpecLoader().load(str(path)) == {"openapi": "3.0.0"}

    def test_extensionless_json(self, tmp_path):
        path = tmp_path / "spec"
        path.write_text('{"openapi": "3.0.0"}')
        assert FileSpecLoader().load(path) == {"openapi": "3.0.0"}

    def test_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            FileSpecLoader().load(tmp_path / "nope.json")

    def test_non_mapping_document(self, tmp_path):
        path = tmp_path / "list.yaml"
        path.write_text("- a\n- b\n")
        with pytest.raises(ValueError, match="Invalid specification format"):
            FileSpecLoader().load(path)


class TestUrlSpecLoader:
    def test_supports_only_http_urls(self):
        loader = UrlSpecLoader()
        assert loader.supports("https://example.com/openapi.json")
        assert loader.supports("http://example.com/openapi.yaml")
        assert not loader.supports("openapi.json")

    def test_rejects_private_hosts_before_any_request(self):
        with pytest.raises(ValueError, match="Unsafe URL"):
            UrlSpecLoader().load("http://127.0.0.1/openapi.json")

    def test_rejects_domain_outside_allowlist(self):
        with pytest.raises(ValueError, match="Unsafe URL"):
            UrlSpecLoader().load("https://example.com/openapi.json", allowed_domains={"other.com"})

    # -- redirects, where the check used to stop --------------------------
    #
    # A spec URL is as redirectable as any other, and the fetch used to follow
    # redirects with the policy applied only to the first hop.

    @staticmethod
    def _recording(handler):
        seen: list[httpx.Request] = []

        def record(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        return httpx.MockTransport(record), seen

    def test_follows_an_allowed_redirect(self):
        target = "https://example.com/v2/openapi.json"

        def handler(request):
            if str(request.url) == target:
                return httpx.Response(200, json={"openapi": "3.0.0"})
            return httpx.Response(302, headers={"Location": target})

        transport, seen = self._recording(handler)
        spec = UrlSpecLoader(transport=transport).load(
            "https://example.com/openapi.json", allowed_domains={"example.com"}
        )

        assert spec == {"openapi": "3.0.0"}
        assert len(seen) == 2

    def test_rejects_a_redirect_to_a_private_address(self):
        transport, seen = self._recording(
            lambda _: httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/"})
        )

        with pytest.raises(ValueError, match="Unsafe redirect"):
            UrlSpecLoader(transport=transport).load("https://example.com/openapi.json")

        assert len(seen) == 1

    def test_rejects_a_redirect_off_the_allowlist(self):
        transport, seen = self._recording(
            lambda _: httpx.Response(302, headers={"Location": "https://evil.com/x.json"})
        )

        with pytest.raises(ValueError, match="Unsafe redirect"):
            UrlSpecLoader(transport=transport).load(
                "https://example.com/openapi.json", allowed_domains={"example.com"}
            )

        assert len(seen) == 1

    def test_caps_a_redirect_chain(self):
        counter = {"n": 0}

        def handler(request):
            counter["n"] += 1
            return httpx.Response(302, headers={"Location": f"/v{counter['n']}/openapi.json"})

        transport, seen = self._recording(handler)

        with pytest.raises(ValueError, match="Too many redirects"):
            UrlSpecLoader(transport=transport).load(
                "https://example.com/openapi.json", allowed_domains={"example.com"}
            )

        assert len(seen) == MAX_REDIRECTS + 1

    def test_a_redirect_to_yaml_is_parsed_as_yaml(self):
        """The format is sniffed on the URL the body came from, not the one asked for."""
        target = "https://example.com/openapi.yaml"

        def handler(request):
            if str(request.url) == target:
                return httpx.Response(200, text="openapi: 3.0.0\n")
            return httpx.Response(302, headers={"Location": target})

        transport, _ = self._recording(handler)
        spec = UrlSpecLoader(transport=transport).load(
            "https://example.com/openapi.json", allowed_domains={"example.com"}
        )

        assert spec == {"openapi": "3.0.0"}


class TestCompositeSpecLoader:
    def test_default_loader_handles_dict_and_file(self):
        loader = default_spec_loader()
        assert loader.load({"openapi": "3.0.0"}) == {"openapi": "3.0.0"}
        assert "paths" in loader.load(str(FIXTURES / "openapi_petstore.json"))

    def test_unsupported_source(self):
        with pytest.raises(ValueError, match="Unsupported specification source"):
            CompositeSpecLoader([DictSpecLoader()]).load("spec.json")
