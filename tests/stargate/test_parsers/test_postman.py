"""Postman parser testleri."""

import json
from pathlib import Path

import pytest

from truejoin.adapters.outbound.mcp_gateway.parsers.postman import PostmanParser
from truejoin.domain.mcp.api import ParameterLocation


@pytest.fixture
def postman_spec():
    """Test Postman Collection."""
    fixtures_path = Path(__file__).parent.parent / "fixtures" / "postman_collection.json"
    return json.loads(fixtures_path.read_text())


@pytest.fixture
def parser():
    """Postman parser instance."""
    return PostmanParser()


class TestPostmanParser:
    """Postman parser testleri."""

    def test_can_parse_postman(self, parser):
        """Postman Collection formatını tanımalı."""
        spec = {
            "info": {
                "_postman_id": "test-123",
                "name": "Test",
            },
            "item": [],
        }
        assert parser.can_parse(spec) is True

    def test_can_parse_postman_schema(self, parser):
        """Postman schema URL'ini tanımalı."""
        spec = {
            "info": {
                "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
            },
            "item": [],
        }
        assert parser.can_parse(spec) is True

    def test_cannot_parse_unknown(self, parser):
        """Bilinmeyen formatı reddetmeli."""
        spec = {"openapi": "3.0.0"}
        assert parser.can_parse(spec) is False

    def test_parse_basic_info(self, parser, postman_spec):
        """Temel bilgileri parse etmeli."""
        schema = parser.parse(postman_spec)

        assert schema.title == "Test API Collection"
        assert schema.version == "1.0.0"

    def test_parse_variables(self, parser, postman_spec):
        """Collection variables'ları parse etmeli."""
        schema = parser.parse(postman_spec)

        # baseUrl variable'ından server oluşturulmalı
        assert len(schema.servers) == 1
        assert schema.servers[0].url == "https://api.example.com"

    def test_parse_endpoints(self, parser, postman_spec):
        """Endpoint'leri parse etmeli."""
        schema = parser.parse(postman_spec)

        # 4 endpoint olmalı (List Users, Get User, Create User, Health Check)
        assert len(schema.endpoints) == 4

    def test_parse_nested_folders(self, parser, postman_spec):
        """Nested folder'ları parse etmeli."""
        schema = parser.parse(postman_spec)

        # Users folder'ındaki endpoint'ler "Users" tag'ine sahip olmalı
        list_users = next(e for e in schema.endpoints if "list" in e.operation_id.lower())
        assert "Users" in list_users.tags

    def test_parse_query_parameters(self, parser, postman_spec):
        """Query parametrelerini parse etmeli."""
        schema = parser.parse(postman_spec)

        list_users = next(e for e in schema.endpoints if "list" in e.operation_id.lower())

        # page ve limit parametreleri olmalı
        param_names = [p.name for p in list_users.parameters]
        assert "page" in param_names
        assert "limit" in param_names

        page_param = next(p for p in list_users.parameters if p.name == "page")
        assert page_param.location == ParameterLocation.QUERY

    def test_parse_path_parameters(self, parser, postman_spec):
        """Path parametrelerini parse etmeli."""
        schema = parser.parse(postman_spec)

        get_user = next(e for e in schema.endpoints if "get_user" in e.operation_id.lower())

        # userId path parametresi olmalı
        user_id_param = next((p for p in get_user.parameters if p.name == "userId"), None)
        assert user_id_param is not None
        assert user_id_param.location == ParameterLocation.PATH
        assert user_id_param.required is True

    def test_parse_request_body(self, parser, postman_spec):
        """Request body'yi parse etmeli."""
        schema = parser.parse(postman_spec)

        create_user = next(e for e in schema.endpoints if "create" in e.operation_id.lower())

        assert create_user.request_body is not None
        assert "application/json" in create_user.request_body.content

    def test_parse_responses(self, parser, postman_spec):
        """Response örneklerini parse etmeli."""
        schema = parser.parse(postman_spec)

        list_users = next(e for e in schema.endpoints if "list" in e.operation_id.lower())

        # Response örneği varsa parse edilmeli
        assert "200" in list_users.responses

    def test_parse_auth(self, parser, postman_spec):
        """Auth bilgilerini parse etmeli."""
        schema = parser.parse(postman_spec)

        assert "bearerAuth" in schema.security_schemes

    def test_extract_tags_from_folders(self, parser, postman_spec):
        """Folder'lardan tag'leri çıkarmalı."""
        schema = parser.parse(postman_spec)

        tag_names = [t["name"] for t in schema.tags]
        assert "Users" in tag_names

    def test_path_variable_conversion(self, parser):
        """Path variable'larını doğru formata dönüştürmeli."""
        spec = {
            "info": {"_postman_id": "test", "name": "Test"},
            "item": [
                {
                    "name": "Get Item",
                    "request": {
                        "method": "GET",
                        "url": {
                            "raw": "https://api.example.com/items/:itemId/details/:detailId",
                            "path": ["items", ":itemId", "details", ":detailId"],
                        },
                    },
                }
            ],
        }

        schema = parser.parse(spec)
        endpoint = schema.endpoints[0]

        # :itemId -> {itemId} dönüşümü
        assert "{itemId}" in endpoint.path
        assert "{detailId}" in endpoint.path
        assert ":itemId" not in endpoint.path

    def test_generate_operation_id(self, parser):
        """Operation ID oluşturmalı."""
        spec = {
            "info": {"_postman_id": "test", "name": "Test"},
            "item": [
                {
                    "name": "Get All Users",
                    "request": {
                        "method": "GET",
                        "url": {"raw": "https://api.example.com/users"},
                    },
                }
            ],
        }

        schema = parser.parse(spec)
        endpoint = schema.endpoints[0]

        # İsimden operation_id oluşturulmalı
        assert endpoint.operation_id == "get_all_users"
