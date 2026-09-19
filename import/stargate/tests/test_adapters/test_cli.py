"""CLI smoke tests via click's CliRunner (no network)."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from stargate.adapters.cli import main
from stargate.adapters.json_server_repository import JsonServerRepository

FIXTURE = str(Path(__file__).parent.parent / "fixtures" / "openapi_petstore.json")


@pytest.fixture
def runner():
    return CliRunner()


def test_formats(runner):
    result = runner.invoke(main, ["formats"])

    assert result.exit_code == 0, result.output
    assert "OpenAPI" in result.output


def test_convert_to_directory_writes_server_json(runner, tmp_path):
    result = runner.invoke(main, ["convert", "-i", FIXTURE, "-o", str(tmp_path)])

    assert result.exit_code == 0, result.output
    server = JsonServerRepository().load(tmp_path / "server.json")
    assert server.name == "Petstore API"
    assert len(server.tools) == 4


def test_convert_json_output_is_wire_format(runner):
    result = runner.invoke(main, ["convert", "-i", FIXTURE, "--json-output"])

    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    assert data["success"] is True
    assert "http_method" not in data["server"]["tools"][0]
    assert "inputSchema" in data["server"]["tools"][0]


def test_info(runner):
    result = runner.invoke(main, ["info", "-i", FIXTURE])

    assert result.exit_code == 0, result.output
    assert "Petstore API" in result.output


def test_convert_requires_a_source(runner):
    result = runner.invoke(main, ["convert"])

    assert result.exit_code == 1
    assert "--input or --url" in result.output


def test_serve_missing_file(runner, tmp_path):
    result = runner.invoke(main, ["serve", "-i", str(tmp_path / "nope.json")])

    assert result.exit_code == 1
    assert "File not found" in result.output


class TestGovernanceFlags:
    def test_convert_can_produce_a_read_only_server(self, tmp_path):
        spec = tmp_path / "api.json"
        spec.write_text(
            json.dumps(
                {
                    "stargate_format": "manual",
                    "name": "Shop",
                    "base_url": "https://api.example.com",
                    "endpoints": [
                        {"path": "/items", "method": "GET", "name": "list_items"},
                        {"path": "/items", "method": "DELETE", "name": "delete_items"},
                    ],
                }
            )
        )
        out = tmp_path / "server.json"
        result = CliRunner().invoke(main, ["convert", "-i", str(spec), "-o", str(out), "-m", "GET"])
        assert result.exit_code == 0, result.output
        assert [t["name"] for t in json.loads(out.read_text())["tools"]] == ["list_items"]

    def test_serve_accepts_a_domain_allowlist(self):
        # The runtime always took allowed_domains; the CLI never passed it, so
        # a served definition could call any public host.
        assert "--allowed-domain" in CliRunner().invoke(main, ["serve", "--help"]).output
