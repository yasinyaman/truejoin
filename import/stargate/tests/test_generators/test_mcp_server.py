"""MCP Server generator testleri."""

import json
from pathlib import Path

import pytest

from stargate.adapters.mcp_wire import server_to_mcp
from stargate.adapters.parsers.openapi import OpenAPIParser
from stargate.generators.mcp_server import MCPServerGenerator, generate_server


@pytest.fixture
def openapi_schema():
    """Test OpenAPI schema."""
    fixtures_path = Path(__file__).parent.parent / "fixtures" / "openapi_petstore.json"
    spec = json.loads(fixtures_path.read_text())
    parser = OpenAPIParser()
    return parser.parse(spec)


@pytest.fixture
def generator():
    """MCP Server generator instance."""
    return MCPServerGenerator()


class TestMCPServerGenerator:
    """MCP Server generator testleri."""

    def test_generate_server(self, generator, openapi_schema):
        """Server oluşturmalı."""
        result = generator.generate(openapi_schema)

        assert result.success is True
        assert result.server is not None

    def test_server_info(self, generator, openapi_schema):
        """Server bilgileri doğru olmalı."""
        result = generator.generate(openapi_schema)
        server = result.server

        assert server.name == "Petstore API"
        assert server.version == "1.0.0"
        assert server.base_url == "https://petstore.example.com/api/v1"

    def test_server_tools(self, generator, openapi_schema):
        """Server tool'ları olmalı."""
        result = generator.generate(openapi_schema)
        server = result.server

        assert len(server.tools) == 4

    def test_server_resources(self, generator, openapi_schema):
        """Server resource'ları olmalı."""
        result = generator.generate(openapi_schema)
        server = result.server

        assert len(server.resources) > 0

    def test_server_security_schemes(self, generator, openapi_schema):
        """Server güvenlik şemaları olmalı."""
        result = generator.generate(openapi_schema)
        server = result.server

        assert "bearerAuth" in server.security_schemes

    def test_metadata(self, generator, openapi_schema):
        """Metadata olmalı."""
        result = generator.generate(openapi_schema)

        assert result.metadata["title"] == "Petstore API"
        assert result.metadata["tool_count"] == 4
        assert result.metadata["resource_count"] > 0

    def test_get_tool(self, generator, openapi_schema):
        """Tool'u isimle bulmalı."""
        result = generator.generate(openapi_schema)
        server = result.server

        tool = server.get_tool("listPets")
        assert tool is not None
        assert tool.name == "listPets"

        not_found = server.get_tool("nonexistent")
        assert not_found is None

    def test_get_resource(self, generator, openapi_schema):
        """Resource'u URI ile bulmalı."""
        result = generator.generate(openapi_schema)
        server = result.server

        resource = server.get_resource("api://pets")
        assert resource is not None
        assert resource.name == "listPets"

        not_found = server.get_resource("api://nonexistent")
        assert not_found is None

    def test_to_mcp_format(self, generator, openapi_schema):
        """MCP formatına dönüştürmeli."""
        result = generator.generate(openapi_schema)
        server = result.server

        mcp_format = server_to_mcp(server)

        assert "name" in mcp_format
        assert "version" in mcp_format
        assert "tools" in mcp_format
        assert "resources" in mcp_format
        assert len(mcp_format["tools"]) == 4

    def test_include_deprecated(self, openapi_schema):
        """include_deprecated seçeneği çalışmalı."""
        generator = MCPServerGenerator(include_deprecated=True)
        result = generator.generate(openapi_schema)

        # deletePet dahil
        assert len(result.server.tools) == 5

    def test_filter_tags(self, openapi_schema):
        """filter_tags seçeneği çalışmalı."""
        generator = MCPServerGenerator(filter_tags=["pets"])
        result = generator.generate(openapi_schema)

        # Tüm endpoint'ler "pets" tag'ine sahip
        assert len(result.server.tools) == 4


class TestGenerateServerHelper:
    """generate_server helper fonksiyonu testleri."""

    def test_generate_server_helper(self, openapi_schema):
        """Helper fonksiyon çalışmalı."""
        result = generate_server(openapi_schema)

        assert result.success is True
        assert len(result.server.tools) == 4

    def test_generate_server_with_options(self, openapi_schema):
        """Helper fonksiyon seçenekleri desteklemeli."""
        result = generate_server(
            openapi_schema,
            include_deprecated=True,
            filter_tags=["pets"],
        )

        assert len(result.server.tools) == 5
