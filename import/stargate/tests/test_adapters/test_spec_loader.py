"""SpecLoader adapter tests (no network)."""

from pathlib import Path

import pytest

from stargate.adapters.spec_loader import (
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


class TestCompositeSpecLoader:
    def test_default_loader_handles_dict_and_file(self):
        loader = default_spec_loader()
        assert loader.load({"openapi": "3.0.0"}) == {"openapi": "3.0.0"}
        assert "paths" in loader.load(str(FIXTURES / "openapi_petstore.json"))

    def test_unsupported_source(self):
        with pytest.raises(ValueError, match="Unsupported specification source"):
            CompositeSpecLoader([DictSpecLoader()]).load("spec.json")
