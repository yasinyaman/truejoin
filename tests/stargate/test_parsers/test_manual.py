"""Manual parser tests."""

import json
from pathlib import Path

import pytest

from stargate.adapters.parsers.manual import ManualParser, create_manual_api
from stargate.domain.api import HTTPMethod, ParameterLocation


@pytest.fixture
def manual_spec():
    """Test manual API definition."""
    fixtures_path = Path(__file__).parent.parent / "fixtures" / "manual_api.json"
    return json.loads(fixtures_path.read_text())


@pytest.fixture
def parser():
    """Manual parser instance."""
    return ManualParser()


class TestManualParser:
    """Manual parser tests."""

    def test_can_parse_manual_format(self, parser):
        """Should recognize manual format."""
        spec = {"format": "manual", "title": "Test", "endpoints": []}
        assert parser.can_parse(spec) is True

    def test_can_parse_stargate_format(self, parser):
        """Should recognize stargate format."""
        spec = {"format": "stargate", "title": "Test", "endpoints": []}
        assert parser.can_parse(spec) is True

    def test_can_parse_simple_format(self, parser):
        """Should recognize simple format."""
        spec = {"base_url": "https://api.example.com", "endpoints": []}
        assert parser.can_parse(spec) is True

    def test_cannot_parse_unknown(self, parser):
        """Should reject unknown format."""
        spec = {"openapi": "3.0.0"}
        assert parser.can_parse(spec) is False

    def test_parse_basic_info(self, parser, manual_spec):
        """Should parse basic info."""
        schema = parser.parse(manual_spec)

        assert schema.title == "Manual Test API"
        assert schema.version == "1.0.0"
        assert "manually defined" in schema.description

    def test_parse_base_url(self, parser, manual_spec):
        """Should parse base URL."""
        schema = parser.parse(manual_spec)

        assert len(schema.servers) == 1
        assert schema.servers[0].url == "https://api.manual-test.com"

    def test_parse_endpoints(self, parser, manual_spec):
        """Should parse endpoints."""
        schema = parser.parse(manual_spec)

        # Should have 4 endpoints
        assert len(schema.endpoints) == 4

        # listUsers check (operation_id is lowercase)
        list_users = next((e for e in schema.endpoints if e.operation_id == "listusers"), None)
        assert list_users is not None
        assert list_users.method == HTTPMethod.GET
        assert list_users.path == "/users"

    def test_parse_parameters(self, parser, manual_spec):
        """Should parse parameters."""
        schema = parser.parse(manual_spec)

        list_users = next((e for e in schema.endpoints if e.operation_id == "listusers"), None)
        assert list_users is not None

        # page and limit parameters
        param_names = [p.name for p in list_users.parameters]
        assert "page" in param_names
        assert "limit" in param_names

    def test_parse_path_parameters_auto(self, parser, manual_spec):
        """Should auto-extract path parameters."""
        schema = parser.parse(manual_spec)

        get_user = next((e for e in schema.endpoints if e.operation_id == "getuser"), None)
        assert get_user is not None

        # {id} path parameter should be auto-extracted
        id_param = next((p for p in get_user.parameters if p.name == "id"), None)
        assert id_param is not None
        assert id_param.location == ParameterLocation.PATH
        assert id_param.required is True

    def test_parse_request_body(self, parser, manual_spec):
        """Should parse request body."""
        schema = parser.parse(manual_spec)

        create_user = next((e for e in schema.endpoints if e.operation_id == "createuser"), None)
        assert create_user is not None

        assert create_user.request_body is not None
        assert "application/json" in create_user.request_body.content

    def test_parse_response_schema(self, parser, manual_spec):
        """Should parse response schema."""
        schema = parser.parse(manual_spec)

        list_users = next((e for e in schema.endpoints if e.operation_id == "listusers"), None)
        assert list_users is not None

        assert "200" in list_users.responses
        content = list_users.responses["200"].content
        assert "application/json" in content

    def test_parse_auth(self, parser, manual_spec):
        """Should parse auth info."""
        schema = parser.parse(manual_spec)

        assert "bearerAuth" in schema.security_schemes

    def test_parse_endpoint_auth(self, parser, manual_spec):
        """Should parse endpoint-level auth."""
        schema = parser.parse(manual_spec)

        create_user = next((e for e in schema.endpoints if e.operation_id == "createuser"), None)
        assert create_user is not None

        # Endpoint with auth: true should have security
        assert len(create_user.security) > 0

    def test_parse_tags(self, parser, manual_spec):
        """Should parse tags."""
        schema = parser.parse(manual_spec)

        list_users = next((e for e in schema.endpoints if e.operation_id == "listusers"), None)
        assert list_users is not None
        assert "users" in list_users.tags

        list_products = next(
            (e for e in schema.endpoints if e.operation_id == "listproducts"), None
        )
        assert list_products is not None
        assert "products" in list_products.tags


class TestCreateManualAPI:
    """create_manual_api helper function tests."""

    def test_create_simple_api(self):
        """Should create simple API."""
        schema = create_manual_api(
            title="Simple API",
            base_url="https://api.simple.com",
            endpoints=[
                {"path": "/items", "method": "GET", "name": "listItems"},
                {"path": "/items/{id}", "method": "GET", "name": "getItem"},
            ],
        )

        assert schema.title == "Simple API"
        assert len(schema.endpoints) == 2

    def test_create_api_with_auth(self):
        """Should create API with auth."""
        schema = create_manual_api(
            title="Auth API",
            base_url="https://api.auth.com",
            endpoints=[
                {"path": "/protected", "method": "GET", "name": "protected"},
            ],
            auth={"type": "bearer"},
        )

        assert "bearerAuth" in schema.security_schemes

    def test_create_api_with_version(self):
        """Should create API with version."""
        schema = create_manual_api(
            title="Versioned API",
            base_url="https://api.versioned.com",
            version="2.0.0",
            endpoints=[],
        )

        assert schema.version == "2.0.0"
