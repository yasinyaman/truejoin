"""Resources generator testleri."""

import json
from pathlib import Path

import pytest

from truejoin.adapters.inbound.mcp.mcp_wire import resource_to_mcp
from truejoin.adapters.outbound.mcp_gateway.parsers.openapi import OpenAPIParser
from truejoin.application.mcpgen.generators.resources import ResourcesGenerator, generate_resources


@pytest.fixture
def openapi_schema():
    """Test OpenAPI schema."""
    fixtures_path = Path(__file__).parent.parent / "fixtures" / "openapi_petstore.json"
    spec = json.loads(fixtures_path.read_text())
    parser = OpenAPIParser()
    return parser.parse(spec)


@pytest.fixture
def generator():
    """Resources generator instance."""
    return ResourcesGenerator()


class TestResourcesGenerator:
    """Resources generator testleri."""

    def test_generate_resources(self, generator, openapi_schema):
        """Resource'ları oluşturmalı."""
        resources = generator.generate(openapi_schema)

        # GET endpoint'leri + schema'lar
        assert len(resources) > 0

    def test_endpoint_resources(self, generator, openapi_schema):
        """GET endpoint'lerinden resource oluşturmalı."""
        resources = generator.generate(openapi_schema)

        # listPets ve getPet GET endpoint'leri
        endpoint_resources = [r for r in resources if r.uri.startswith("api://")]
        assert len(endpoint_resources) == 2

    def test_schema_resources(self, generator, openapi_schema):
        """Schema'lardan resource oluşturmalı."""
        resources = generator.generate(openapi_schema)

        # Pet ve NewPet schema'ları
        schema_resources = [r for r in resources if r.uri.startswith("schema://")]
        assert len(schema_resources) == 2

        pet_schema = next(r for r in schema_resources if "pet" in r.uri.lower())
        assert pet_schema.schema_def is not None

    def test_resource_uri(self, generator, openapi_schema):
        """Resource URI'leri doğru olmalı."""
        resources = generator.generate(openapi_schema)

        list_pets = next(r for r in resources if r.name == "listPets")
        assert list_pets.uri == "api://pets"

    def test_resource_description(self, generator, openapi_schema):
        """Resource açıklamaları olmalı."""
        resources = generator.generate(openapi_schema)

        list_pets = next(r for r in resources if r.name == "listPets")
        assert list_pets.description != ""

    def test_resource_mime_type(self, generator, openapi_schema):
        """Resource MIME tipi olmalı."""
        resources = generator.generate(openapi_schema)

        for resource in resources:
            assert resource.mime_type == "application/json"

    def test_resource_http_path(self, generator, openapi_schema):
        """Endpoint resource'larında HTTP path olmalı."""
        resources = generator.generate(openapi_schema)

        list_pets = next(r for r in resources if r.name == "listPets")
        assert list_pets.http_path == "/pets"

    def test_exclude_get_endpoints(self, openapi_schema):
        """GET endpoint'lerini hariç tutabilmeli."""
        generator = ResourcesGenerator(include_get_endpoints=False)
        resources = generator.generate(openapi_schema)

        endpoint_resources = [r for r in resources if r.uri.startswith("api://")]
        assert len(endpoint_resources) == 0

    def test_exclude_schemas(self, openapi_schema):
        """Schema'ları hariç tutabilmeli."""
        generator = ResourcesGenerator(include_schemas=False)
        resources = generator.generate(openapi_schema)

        schema_resources = [r for r in resources if r.uri.startswith("schema://")]
        assert len(schema_resources) == 0

    def test_filter_by_tags(self, openapi_schema):
        """Tag'e göre filtrelemeli."""
        generator = ResourcesGenerator(filter_tags=["pets"])
        resources = generator.generate(openapi_schema)

        # Tüm endpoint'ler "pets" tag'ine sahip
        endpoint_resources = [r for r in resources if r.uri.startswith("api://")]
        assert len(endpoint_resources) == 2

    def test_to_mcp_format(self, generator, openapi_schema):
        """MCP formatına dönüştürmeli."""
        resources = generator.generate(openapi_schema)

        list_pets = next(r for r in resources if r.name == "listPets")
        mcp_format = resource_to_mcp(list_pets)

        assert "name" in mcp_format
        assert "description" in mcp_format
        assert "uri" in mcp_format
        assert "mimeType" in mcp_format
        # Dahili alanlar olmamalı
        assert "http_path" not in mcp_format


class TestGenerateResourcesHelper:
    """generate_resources helper fonksiyonu testleri."""

    def test_generate_resources_helper(self, openapi_schema):
        """Helper fonksiyon çalışmalı."""
        resources = generate_resources(openapi_schema)

        assert len(resources) > 0

    def test_generate_resources_with_options(self, openapi_schema):
        """Helper fonksiyon seçenekleri desteklemeli."""
        resources = generate_resources(
            openapi_schema,
            include_schemas=False,
            include_get_endpoints=True,
        )

        # Sadece endpoint resource'ları
        assert all(r.uri.startswith("api://") for r in resources)
