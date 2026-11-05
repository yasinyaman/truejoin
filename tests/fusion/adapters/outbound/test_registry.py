"""Tests for the source registry."""

import pytest

from tests.fusion.fakes import FakeDataSource, FakeWarpTransport
from trio.adapters.outbound.registry import SourceRegistry, default_registry
from trio.adapters.outbound.warp_http.source import WarpSource
from trio.domain.olap.errors import ConnectionError


class TestSourceRegistry:
    def test_register_and_create(self):
        registry = SourceRegistry()
        registry.register("fake", lambda name, cfg: FakeDataSource(name, cfg.get("tables")))
        source = registry.create("s1", {"type": "fake", "tables": {"t": [{"a": 1}]}})
        assert isinstance(source, FakeDataSource)
        assert source.name == "s1"
        assert registry.types() == ["fake"]

    def test_missing_type(self):
        with pytest.raises(ConnectionError, match="type"):
            SourceRegistry().create("s", {})

    def test_unknown_type(self):
        registry = SourceRegistry()
        registry.register("fake", lambda n, c: FakeDataSource(n))
        with pytest.raises(ConnectionError, match="Unknown source type 'nope'"):
            registry.create("s", {"type": "nope"})


class TestDefaultRegistry:
    def test_has_both_warp_sources(self):
        assert default_registry().types() == ["warp", "warp-remote"]

    def test_in_process_warp_needs_a_gateway(self):
        with pytest.raises(ConnectionError, match="warp-remote"):
            default_registry().create("db", {"type": "warp", "base_url": "http://localhost:1"})

    def test_builds_remote_warp_source_with_merged_defaults(self):
        transport = FakeWarpTransport({"t": []}, database="db")
        registry = default_registry({"timeout": 7, "page_size": 3})
        source = registry.create(
            "db", {"type": "warp-remote", "base_url": "http://localhost:1", "transport": transport}
        )
        assert isinstance(source, WarpSource)
        assert source.database == "db"
        assert source._page_size == 3
        assert source._client.timeout == 7
