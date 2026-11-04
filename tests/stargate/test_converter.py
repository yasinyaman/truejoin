"""Main converter module tests."""

import json
from pathlib import Path

import pytest

from trio.adapters.outbound.mcp_gateway.parsers.manual import ManualParser
from trio.adapters.outbound.mcp_gateway.parsers.openapi import OpenAPIParser
from trio.adapters.outbound.mcp_gateway.parsers.postman import PostmanParser
from trio.bootstrap_mcpgen import detect_format
from trio.mcpgen import convert_to_mcp, parse_spec


@pytest.fixture
def fixtures_path():
    """Fixtures directory."""
    return Path(__file__).parent / "fixtures"


class TestDetectFormat:
    """Format detection tests."""

    def test_detect_openapi(self):
        """Should detect OpenAPI format."""
        spec = {"openapi": "3.0.0", "info": {"title": "Test", "version": "1.0.0"}}
        parser = detect_format(spec)

        assert isinstance(parser, OpenAPIParser)

    def test_detect_postman(self):
        """Should detect Postman format."""
        spec = {"info": {"_postman_id": "test"}, "item": []}
        parser = detect_format(spec)

        assert isinstance(parser, PostmanParser)

    def test_detect_manual(self):
        """Should detect manual format."""
        spec = {"format": "manual", "endpoints": []}
        parser = detect_format(spec)

        assert isinstance(parser, ManualParser)

    def test_detect_unknown(self):
        """Should not detect unknown format."""
        spec = {"unknown": "format"}
        parser = detect_format(spec)

        assert parser is None


class TestConvertToMCP:
    """convert_to_mcp function tests."""

    def test_convert_from_dict(self, fixtures_path):
        """Should convert from dict."""
        spec = json.loads((fixtures_path / "openapi_petstore.json").read_text())
        result = convert_to_mcp(spec)

        assert result.success is True
        assert len(result.server.tools) == 4

    def test_convert_from_file(self, fixtures_path):
        """Should convert from file."""
        result = convert_to_mcp(fixtures_path / "openapi_petstore.json")

        assert result.success is True
        assert result.server.name == "Petstore API"

    def test_convert_with_base_url(self, fixtures_path):
        """Base URL override should work."""
        spec = {
            "format": "manual",
            "title": "Test API",
            "endpoints": [
                {"path": "/test", "method": "GET", "name": "test"},
            ],
        }
        result = convert_to_mcp(spec, base_url="https://custom.api.com")

        assert result.server.base_url == "https://custom.api.com"

    def test_convert_with_format(self, fixtures_path):
        """Format specification should work."""
        spec = json.loads((fixtures_path / "openapi_petstore.json").read_text())
        result = convert_to_mcp(spec, format="openapi")

        assert result.success is True

    def test_convert_with_filter_tags(self, fixtures_path):
        """Tag filtering should work."""
        spec = json.loads((fixtures_path / "openapi_petstore.json").read_text())
        result = convert_to_mcp(spec, filter_tags=["pets"])

        assert result.success is True
        assert len(result.server.tools) == 4

    def test_convert_with_include_deprecated(self, fixtures_path):
        """Include deprecated should work."""
        spec = json.loads((fixtures_path / "openapi_petstore.json").read_text())
        result = convert_to_mcp(spec, include_deprecated=True)

        assert len(result.server.tools) == 5

    def test_convert_invalid_format(self):
        """Should raise error for invalid format."""
        spec = {"unknown": "format"}

        with pytest.raises(ValueError, match="Could not detect"):
            convert_to_mcp(spec)

    def test_convert_file_not_found(self):
        """Should raise error when file not found."""
        with pytest.raises(FileNotFoundError):
            convert_to_mcp("/nonexistent/path.json")


class TestParseSpec:
    """parse_spec function tests."""

    def test_parse_openapi(self, fixtures_path):
        """Should parse OpenAPI."""
        schema = parse_spec(fixtures_path / "openapi_petstore.json")

        assert schema.title == "Petstore API"
        assert len(schema.endpoints) == 5

    def test_parse_postman(self, fixtures_path):
        """Should parse Postman."""
        schema = parse_spec(fixtures_path / "postman_collection.json")

        assert schema.title == "Test API Collection"
        assert len(schema.endpoints) == 4

    def test_parse_manual(self, fixtures_path):
        """Should parse manual definition."""
        schema = parse_spec(fixtures_path / "manual_api.json")

        assert schema.title == "Manual Test API"
        assert len(schema.endpoints) == 4

    def test_parse_with_format(self, fixtures_path):
        """Format specification should work."""
        schema = parse_spec(
            fixtures_path / "openapi_petstore.json",
            format="openapi",
        )

        assert schema.title == "Petstore API"
