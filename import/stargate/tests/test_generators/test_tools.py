"""Tools generator testleri."""

import json
from pathlib import Path

import pytest

from stargate.adapters.mcp_wire import tool_to_mcp
from stargate.adapters.parsers.openapi import OpenAPIParser
from stargate.application.generators.tools import ToolsGenerator, generate_tools
from stargate.domain.api import HTTPMethod, ParameterLocation


@pytest.fixture
def openapi_schema():
    """Test OpenAPI schema."""
    fixtures_path = Path(__file__).parent.parent / "fixtures" / "openapi_petstore.json"
    spec = json.loads(fixtures_path.read_text())
    parser = OpenAPIParser()
    return parser.parse(spec)


@pytest.fixture
def generator():
    """Tools generator instance."""
    return ToolsGenerator()


class TestToolsGenerator:
    """Tools generator testleri."""

    def test_generate_tools(self, generator, openapi_schema):
        """Tool'ları oluşturmalı."""
        tools = generator.generate(openapi_schema)

        # 4 tool olmalı (deprecated deletePet hariç)
        assert len(tools) == 4

    def test_tool_names(self, generator, openapi_schema):
        """Tool adları doğru olmalı."""
        tools = generator.generate(openapi_schema)
        tool_names = [t.name for t in tools]

        assert "listPets" in tool_names
        assert "createPet" in tool_names
        assert "getPet" in tool_names
        assert "updatePet" in tool_names

    def test_tool_description(self, generator, openapi_schema):
        """Tool açıklamaları olmalı."""
        tools = generator.generate(openapi_schema)

        list_pets = next(t for t in tools if t.name == "listPets")
        assert "List all pets" in list_pets.description
        assert "GET" in list_pets.description

    def test_tool_input_schema(self, generator, openapi_schema):
        """Tool input schema'sı olmalı."""
        tools = generator.generate(openapi_schema)

        list_pets = next(t for t in tools if t.name == "listPets")
        input_schema = list_pets.input_schema

        assert input_schema["type"] == "object"
        assert "properties" in input_schema
        assert "limit" in input_schema["properties"]
        assert "status" in input_schema["properties"]

    def test_tool_http_info(self, generator, openapi_schema):
        """Tool HTTP bilgileri olmalı."""
        tools = generator.generate(openapi_schema)

        list_pets = next(t for t in tools if t.name == "listPets")

        assert list_pets.http_method == "GET"
        assert list_pets.http_path == "/pets"
        assert list_pets.base_url == "https://petstore.example.com/api/v1"

    def test_tool_security(self, generator, openapi_schema):
        """Tool güvenlik bilgileri olmalı."""
        tools = generator.generate(openapi_schema)

        create_pet = next(t for t in tools if t.name == "createPet")

        assert len(create_pet.security) == 1
        assert create_pet.security[0]["scheme"] == "bearerAuth"

    def test_exclude_deprecated(self, generator, openapi_schema):
        """Deprecated endpoint'leri hariç tutmalı."""
        tools = generator.generate(openapi_schema)
        tool_names = [t.name for t in tools]

        assert "deletePet" not in tool_names

    def test_include_deprecated(self, openapi_schema):
        """include_deprecated ile deprecated endpoint'leri dahil etmeli."""
        generator = ToolsGenerator(include_deprecated=True)
        tools = generator.generate(openapi_schema)
        tool_names = [t.name for t in tools]

        assert "deletePet" in tool_names

    def test_filter_by_tags(self, openapi_schema):
        """Tag'e göre filtrelemeli."""
        generator = ToolsGenerator(filter_tags=["pets"])
        tools = generator.generate(openapi_schema)

        # Tüm endpoint'ler "pets" tag'ine sahip
        assert len(tools) == 4

    def test_filter_by_methods(self, openapi_schema):
        """HTTP metoduna göre filtrelemeli."""
        generator = ToolsGenerator(filter_methods=[HTTPMethod.GET])
        tools = generator.generate(openapi_schema)

        # Sadece GET endpoint'leri
        assert len(tools) == 2
        assert all(t.http_method == "GET" for t in tools)

    def test_to_mcp_format(self, generator, openapi_schema):
        """MCP formatına dönüştürmeli."""
        tools = generator.generate(openapi_schema)

        list_pets = next(t for t in tools if t.name == "listPets")
        mcp_format = tool_to_mcp(list_pets)

        assert "name" in mcp_format
        assert "description" in mcp_format
        assert "inputSchema" in mcp_format
        # Dahili alanlar olmamalı
        assert "http_method" not in mcp_format

    def test_parameter_locations(self, generator, openapi_schema):
        """Parametre konumları yapısal alan olarak taşınmalı."""
        tools = generator.generate(openapi_schema)

        for tool in tools:
            assert set(tool.parameter_locations) <= set(tool.input_schema["properties"])
            for name, location in tool.parameter_locations.items():
                if location == ParameterLocation.PATH:
                    assert f"{{{name}}}" in tool.http_path

        assert any(ParameterLocation.PATH in t.parameter_locations.values() for t in tools)
        assert any(ParameterLocation.QUERY in t.parameter_locations.values() for t in tools)


class TestGenerateToolsHelper:
    """generate_tools helper fonksiyonu testleri."""

    def test_generate_tools_helper(self, openapi_schema):
        """Helper fonksiyon çalışmalı."""
        tools = generate_tools(openapi_schema)

        assert len(tools) == 4

    def test_generate_tools_with_options(self, openapi_schema):
        """Helper fonksiyon seçenekleri desteklemeli."""
        tools = generate_tools(
            openapi_schema,
            include_deprecated=True,
            filter_tags=["pets"],
        )

        assert len(tools) == 5  # deletePet dahil
