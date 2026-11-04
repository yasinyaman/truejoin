"""server.json repository tests (tmp_path only)."""

import json
from pathlib import Path

import pytest

from trio.adapters.outbound.mcp_gateway.json_server_repository import JsonServerRepository
from trio.application.mcpgen.invoke import resource_http_path, separate_params
from trio.domain.mcp.mcp import MCPServer
from trio.mcpgen import convert_to_mcp

FIXTURES = Path(__file__).parent.parent / "fixtures"


@pytest.fixture
def repo():
    return JsonServerRepository()


@pytest.fixture
def petstore_server():
    return convert_to_mcp(FIXTURES / "openapi_petstore.json").server


class TestSave:
    def test_directory_target_writes_server_json(self, repo, petstore_server, tmp_path):
        written = repo.save(petstore_server, tmp_path)

        assert written == tmp_path / "server.json"
        assert written.exists()

    def test_extensionless_target_treated_as_directory(self, repo, petstore_server, tmp_path):
        written = repo.save(petstore_server, tmp_path / "out")

        assert written == tmp_path / "out" / "server.json"

    def test_explicit_file_target_creates_parents(self, repo, petstore_server, tmp_path):
        written = repo.save(petstore_server, tmp_path / "nested" / "my.json")

        assert written == tmp_path / "nested" / "my.json"
        assert written.exists()

    def test_document_layout(self, repo, petstore_server, tmp_path):
        document = json.loads(repo.save(petstore_server, tmp_path).read_text())

        assert document["stargate_format"] == 1
        assert document["name"] == petstore_server.name
        tool = document["tools"][0]
        assert {"name", "description", "inputSchema", "http_method", "http_path"} <= set(tool)
        assert "input_schema" not in tool
        assert "parameter_locations" in tool
        assert None not in tool.values()
        resource = document["resources"][0]
        assert "mimeType" in resource and "mime_type" not in resource


class TestLoad:
    def test_round_trip_preserves_server(self, repo, petstore_server, tmp_path):
        loaded = repo.load(repo.save(petstore_server, tmp_path))

        assert loaded == petstore_server

    def test_accepts_field_names_and_aliases(self, repo, tmp_path):
        path = tmp_path / "s.json"
        path.write_text(
            json.dumps(
                {
                    "name": "S",
                    "tools": [{"name": "a", "input_schema": {"type": "object"}}],
                    "resources": [
                        {"name": "r1", "uri": "api://r1", "mime_type": "text/plain"},
                        {"name": "r2", "uri": "api://r2", "mimeType": "text/csv"},
                    ],
                }
            )
        )

        server = repo.load(path)

        assert server.tools[0].input_schema == {"type": "object"}
        assert [r.mime_type for r in server.resources] == ["text/plain", "text/csv"]

    def test_legacy_layout_loads_and_routes_via_description_hints(self, repo):
        server = repo.load(FIXTURES / "server_legacy.json")

        assert isinstance(server, MCPServer)
        tool = server.get_tool("getProductById")
        assert tool is not None
        assert tool.parameter_locations == {}
        path, query, _, _ = separate_params(tool, {"id": 1})
        assert path == {"id": 1} and query == {}

        resource = server.get_resource("api://products/{id}")
        assert resource is not None and resource.http_path is None
        assert resource_http_path(resource) == "/products/{id}"

    def test_missing_file(self, repo, tmp_path):
        with pytest.raises(FileNotFoundError):
            repo.load(tmp_path / "missing.json")
