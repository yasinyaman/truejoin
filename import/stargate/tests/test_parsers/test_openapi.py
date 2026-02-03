"""OpenAPI parser testleri."""

import json
from pathlib import Path

import pytest

from stargate.models.unified import HTTPMethod, ParameterLocation
from stargate.parsers.openapi import OpenAPIParser


@pytest.fixture
def openapi_spec():
    """Test OpenAPI spesifikasyonu."""
    fixtures_path = Path(__file__).parent.parent / "fixtures" / "openapi_petstore.json"
    return json.loads(fixtures_path.read_text())


@pytest.fixture
def parser():
    """OpenAPI parser instance."""
    return OpenAPIParser()


class TestOpenAPIParser:
    """OpenAPI parser testleri."""

    def test_can_parse_openapi_3(self, parser):
        """OpenAPI 3.x formatını tanımalı."""
        spec = {"openapi": "3.0.0", "info": {"title": "Test", "version": "1.0.0"}}
        assert parser.can_parse(spec) is True

    def test_can_parse_swagger_2(self, parser):
        """Swagger 2.0 formatını tanımalı."""
        spec = {"swagger": "2.0", "info": {"title": "Test", "version": "1.0.0"}}
        assert parser.can_parse(spec) is True

    def test_cannot_parse_unknown(self, parser):
        """Bilinmeyen formatı reddetmeli."""
        spec = {"unknown": "format"}
        assert parser.can_parse(spec) is False

    def test_parse_basic_info(self, parser, openapi_spec):
        """Temel bilgileri doğru parse etmeli."""
        schema = parser.parse(openapi_spec)

        assert schema.title == "Petstore API"
        assert schema.version == "1.0.0"
        assert "Pet Store" in schema.description

    def test_parse_servers(self, parser, openapi_spec):
        """Sunucu bilgilerini parse etmeli."""
        schema = parser.parse(openapi_spec)

        assert len(schema.servers) == 1
        assert schema.servers[0].url == "https://petstore.example.com/api/v1"

    def test_parse_endpoints(self, parser, openapi_spec):
        """Endpoint'leri parse etmeli."""
        schema = parser.parse(openapi_spec)

        # 5 endpoint olmalı (listPets, createPet, getPet, updatePet, deletePet)
        assert len(schema.endpoints) == 5

        # listPets kontrolü
        list_pets = next(e for e in schema.endpoints if e.operation_id == "listPets")
        assert list_pets.method == HTTPMethod.GET
        assert list_pets.path == "/pets"
        assert list_pets.summary == "List all pets"

    def test_parse_parameters(self, parser, openapi_spec):
        """Parametreleri parse etmeli."""
        schema = parser.parse(openapi_spec)

        list_pets = next(e for e in schema.endpoints if e.operation_id == "listPets")

        # 2 query parametresi olmalı
        assert len(list_pets.parameters) == 2

        limit_param = next(p for p in list_pets.parameters if p.name == "limit")
        assert limit_param.location == ParameterLocation.QUERY
        assert limit_param.required is False
        assert limit_param.schema_["type"] == "integer"

    def test_parse_path_parameters(self, parser, openapi_spec):
        """Path parametrelerini parse etmeli."""
        schema = parser.parse(openapi_spec)

        get_pet = next(e for e in schema.endpoints if e.operation_id == "getPet")

        pet_id_param = next(p for p in get_pet.parameters if p.name == "petId")
        assert pet_id_param.location == ParameterLocation.PATH
        assert pet_id_param.required is True

    def test_parse_request_body(self, parser, openapi_spec):
        """Request body'yi parse etmeli."""
        schema = parser.parse(openapi_spec)

        create_pet = next(e for e in schema.endpoints if e.operation_id == "createPet")

        assert create_pet.request_body is not None
        assert create_pet.request_body.required is True
        assert "application/json" in create_pet.request_body.content

    def test_parse_responses(self, parser, openapi_spec):
        """Response'ları parse etmeli."""
        schema = parser.parse(openapi_spec)

        list_pets = next(e for e in schema.endpoints if e.operation_id == "listPets")

        assert "200" in list_pets.responses
        assert list_pets.responses["200"].description == "A list of pets"

    def test_parse_security(self, parser, openapi_spec):
        """Güvenlik gereksinimlerini parse etmeli."""
        schema = parser.parse(openapi_spec)

        create_pet = next(e for e in schema.endpoints if e.operation_id == "createPet")

        assert len(create_pet.security) == 1
        assert create_pet.security[0].scheme_name == "bearerAuth"

    def test_parse_security_schemes(self, parser, openapi_spec):
        """Güvenlik şemalarını parse etmeli."""
        schema = parser.parse(openapi_spec)

        assert "bearerAuth" in schema.security_schemes
        assert schema.security_schemes["bearerAuth"].scheme == "bearer"

    def test_parse_schemas(self, parser, openapi_spec):
        """Schema tanımlarını parse etmeli."""
        schema = parser.parse(openapi_spec)

        assert "Pet" in schema.schemas
        assert "NewPet" in schema.schemas
        assert schema.schemas["Pet"]["type"] == "object"

    def test_parse_tags(self, parser, openapi_spec):
        """Tag'leri parse etmeli."""
        schema = parser.parse(openapi_spec)

        list_pets = next(e for e in schema.endpoints if e.operation_id == "listPets")
        assert "pets" in list_pets.tags

    def test_parse_deprecated(self, parser, openapi_spec):
        """Deprecated flag'ini parse etmeli."""
        schema = parser.parse(openapi_spec)

        delete_pet = next(e for e in schema.endpoints if e.operation_id == "deletePet")
        assert delete_pet.deprecated is True

        list_pets = next(e for e in schema.endpoints if e.operation_id == "listPets")
        assert list_pets.deprecated is False

    def test_get_base_url(self, parser, openapi_spec):
        """Base URL'i döndürmeli."""
        schema = parser.parse(openapi_spec)

        assert schema.get_base_url() == "https://petstore.example.com/api/v1"

    def test_get_endpoints_by_tag(self, parser, openapi_spec):
        """Tag'e göre endpoint filtrelemeli."""
        schema = parser.parse(openapi_spec)

        pets_endpoints = schema.get_endpoints_by_tag("pets")
        assert len(pets_endpoints) == 5

    def test_get_endpoints_by_method(self, parser, openapi_spec):
        """HTTP metoduna göre endpoint filtrelemeli."""
        schema = parser.parse(openapi_spec)

        get_endpoints = schema.get_endpoints_by_method(HTTPMethod.GET)
        assert len(get_endpoints) == 2  # listPets, getPet
