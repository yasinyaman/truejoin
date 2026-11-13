"""ConversionService / ParserRegistry tests with fake ports (no I/O)."""

from typing import Any, ClassVar

import pytest

from truejoin.application.mcpgen.convert import ConversionService, ParserRegistry
from truejoin.application.mcpgen.generators.server import MCPServerGenerator
from truejoin.domain.mcp.api import HTTPMethod, UnifiedAPISchema, UnifiedEndpoint


class FakeLoader:
    def __init__(self, spec: dict[str, Any]):
        self.spec = spec
        self.calls: list[tuple[Any, set[str] | None]] = []

    def supports(self, source) -> bool:
        return True

    def load(self, source, allowed_domains=None) -> dict[str, Any]:
        self.calls.append((source, allowed_domains))
        return self.spec


class FakeParser:
    format_name: ClassVar[str] = "fake"

    def __init__(self):
        self.warnings: list[str] = []
        self.errors: list[str] = []

    def can_parse(self, spec: dict[str, Any]) -> bool:
        return spec.get("kind") == "fake"

    def parse(self, spec: dict[str, Any]) -> UnifiedAPISchema:
        self.warnings.append("fake warning")
        return UnifiedAPISchema(
            title="Fake API",
            endpoints=[
                UnifiedEndpoint(path="/things", method=HTTPMethod.GET, operation_id="listThings")
            ],
        )


class NeverParser(FakeParser):
    format_name = "never"

    def can_parse(self, spec: dict[str, Any]) -> bool:
        return False


class FakeOpenAPIParser(FakeParser):
    format_name = "openapi"


def make_service(spec: dict[str, Any], **kwargs) -> tuple[ConversionService, FakeLoader]:
    loader = FakeLoader(spec)
    registry = ParserRegistry([NeverParser, FakeParser])
    service = ConversionService(loader=loader, parsers=registry, **kwargs)
    return service, loader


class TestParserRegistry:
    def test_detect_in_priority_order(self):
        parser = ParserRegistry([NeverParser, FakeParser]).detect({"kind": "fake"})
        assert isinstance(parser, FakeParser)

    def test_detect_unknown_returns_none(self):
        assert ParserRegistry([FakeParser]).detect({"kind": "other"}) is None

    def test_for_format_is_case_insensitive(self):
        assert isinstance(ParserRegistry([FakeParser]).for_format("FAKE"), FakeParser)

    def test_for_format_resolves_alias(self):
        registry = ParserRegistry([FakeOpenAPIParser])
        assert isinstance(registry.for_format("swagger"), FakeOpenAPIParser)

    def test_for_format_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown format: nope"):
            ParserRegistry([FakeParser]).for_format("nope")

    def test_formats(self):
        assert ParserRegistry([NeverParser, FakeParser]).formats() == ["never", "fake"]


class TestConversionService:
    def test_convert_uses_injected_loader_and_parser(self):
        service, loader = make_service({"kind": "fake"})

        result = service.convert("any-source", allowed_domains={"api.example.com"})

        assert loader.calls == [("any-source", {"api.example.com"})]
        assert result.success is True
        assert result.server.name == "Fake API"
        assert [t.name for t in result.server.tools] == ["listThings"]
        assert "fake warning" in result.warnings

    def test_base_url_override_when_schema_has_no_servers(self):
        service, _ = make_service({"kind": "fake"})

        result = service.convert({}, base_url="https://api.example.com")

        assert result.server.base_url == "https://api.example.com"

    def test_explicit_format_skips_detection(self):
        service, _ = make_service({"kind": "not-detectable"})

        result = service.convert({}, format="fake")

        assert result.server.name == "Fake API"

    def test_undetectable_format_raises(self):
        service, _ = make_service({"kind": "not-detectable"})

        with pytest.raises(ValueError, match="Could not detect"):
            service.convert({})

    def test_parse_returns_schema_only(self):
        service, _ = make_service({"kind": "fake"})

        schema = service.parse({})

        assert isinstance(schema, UnifiedAPISchema)
        assert schema.title == "Fake API"

    def test_generator_factory_injection(self):
        captured: dict[str, Any] = {}

        def factory(**kwargs):
            captured.update(kwargs)
            return MCPServerGenerator(**kwargs)

        service, _ = make_service({"kind": "fake"}, generator_factory=factory)
        service.convert({}, include_deprecated=True, filter_tags=["a"])

        assert captured == {
            "include_deprecated": True,
            "filter_tags": ["a"],
            "filter_methods": None,
        }
