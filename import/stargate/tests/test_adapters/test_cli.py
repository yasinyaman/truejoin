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


def test_serve_help_documents_the_domain_default(runner):
    """The default is the definition's own hosts, and the escape hatch is named."""
    result = runner.invoke(main, ["serve", "--help"])
    assert "--allowed-domain" in result.output
    assert "--allow-any-domain" in result.output


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


class TestServeDomainDefault:
    """Serving restricts tool calls to the hosts the definition declares.

    It used to default to any public host, so the restriction existed only for
    an operator who knew to ask for it.
    """

    def _saved(self, tmp_path, base_url):
        from stargate.domain.mcp import MCPServer, MCPTool

        server = MCPServer(
            name="api",
            base_url=base_url,
            tools=[MCPTool(name="get_pets", description="d", path="/pets")],
        )
        path = tmp_path / "server.json"
        JsonServerRepository().save(server, path)
        return path

    def test_help_documents_both_switches(self, runner):
        result = runner.invoke(main, ["serve", "--help"])
        assert "--allowed-domain" in result.output
        assert "--allow-any-domain" in result.output

    def test_a_definition_with_no_usable_host_is_refused(self, runner, tmp_path):
        """Falling back to "any public host" here would hand the broadest
        policy to the most malformed input."""
        path = self._saved(tmp_path, "api.example.com/v1")  # no scheme

        result = runner.invoke(main, ["serve", "-i", str(path)])

        assert result.exit_code == 1
        assert "no usable host" in result.output
        assert "api.example.com/v1" in result.output

    def test_the_refusal_can_be_overridden(self, runner, tmp_path):
        """--allow-any-domain is the documented way past it, and it must not
        be the thing that happens by accident."""
        path = self._saved(tmp_path, "api.example.com/v1")

        result = runner.invoke(main, ["serve", "-i", str(path), "--allow-any-domain"])

        assert "no usable host" not in result.output
