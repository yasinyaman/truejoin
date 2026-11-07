"""The one ``trio`` command tree."""

import sys

import pytest
import yaml
from click.testing import CliRunner

from trio.adapters.inbound.cli import main as cli
from trio.application.settings import MASK
from trio.domain.governance.row_policy import PolicyError


@pytest.fixture
def runner():
    return CliRunner()


def _config(tmp_path, **blocks) -> str:
    config = {
        "databases": [
            {
                "name": "shop",
                "type": "postgresql",
                "database": "shop",
                "username": "u",
                "password": "db-pass",
            }
        ],
        "settings": {"catalog": {"storage_path": str(tmp_path / "catalogs")}},
        **blocks,
    }
    path = tmp_path / "trio.yaml"
    path.write_text(yaml.safe_dump(config))
    return str(path)


class TestTree:
    def test_the_four_branches(self, runner):
        assert set(cli.main.commands) == {"serve", "catalog", "mcp", "config"}
        assert set(cli.main.commands["catalog"].commands) == {
            "analyze",
            "review",
            "approve",
            "export",
            "list",
            "info",
            "enrich-openapi",
            "pipeline",
            "suggest",
            "certify",
        }
        assert set(cli.main.commands["mcp"].commands) == {"convert", "info", "formats"}
        assert set(cli.main.commands["config"].commands) == {"validate", "show"}

    def test_version(self, runner):
        result = runner.invoke(cli.main, ["--version"])
        assert result.output.startswith("trio, version ")

    def test_the_branches_run_their_origin_commands(self, runner, tmp_path):
        listed = runner.invoke(cli.main, ["catalog", "-c", _config(tmp_path), "list"])
        assert listed.exit_code == 0, listed.output
        formats = runner.invoke(cli.main, ["mcp", "formats"])
        assert formats.exit_code == 0 and "openapi" in formats.output.lower()

    def test_catalog_approve(self, runner, tmp_path):
        from tests.warp.unit.adapters.inbound.cli.test_cli import _seed_catalog

        store = _seed_catalog(tmp_path / "catalogs")
        config = _config(tmp_path)
        result = runner.invoke(cli.main, ["catalog", "-c", config, "approve", "-d", "testdb"])
        assert result.exit_code == 0, result.output
        assert store.load("testdb").status.value == "approved"
        missing = runner.invoke(cli.main, ["catalog", "-c", config, "approve", "-d", "nope"])
        assert missing.exit_code == 1


class TestConfig:
    def test_show_masks_every_secret(self, runner, tmp_path):
        config = _config(
            tmp_path,
            settings={"auth": {"api_keys": [{"key": "api-secret", "name": "svc"}]}},
            olap={"threads": 2},
        )
        result = runner.invoke(cli.main, ["config", "show", "-c", config])
        assert result.exit_code == 0, result.output
        assert "db-pass" not in result.output and "api-secret" not in result.output
        shown = yaml.safe_load(result.output)
        assert shown["databases"][0]["password"] == MASK
        assert shown["settings"]["auth"]["api_keys"][0] == {
            **shown["settings"]["auth"]["api_keys"][0],
            "key": MASK,
            "name": "svc",
        }
        assert shown["olap"]["threads"] == 2 and shown["mcp"]["http_path"] == "/mcp"

    def test_show_applies_the_legacy_environment(self, runner, tmp_path):
        result = runner.invoke(
            cli.main, ["config", "show", "-c", _config(tmp_path)], env={"FUSION_THREADS": "3"}
        )
        assert yaml.safe_load(result.output)["olap"]["threads"] == 3

    def test_validate_accepts_a_good_config(self, runner, tmp_path):
        result = runner.invoke(cli.main, ["config", "validate", "-c", _config(tmp_path)])
        assert result.exit_code == 0, result.output
        assert "valid (1 database(s), APP_ENV=development)" in result.output

    def test_validate_reads_the_path_from_the_environment(self, runner, tmp_path):
        result = runner.invoke(
            cli.main, ["config", "validate"], env={"CONFIG_PATH": _config(tmp_path)}
        )
        assert result.exit_code == 0, result.output

    def test_validate_production_lists_what_is_unsafe(self, runner, tmp_path):
        result = runner.invoke(
            cli.main, ["config", "validate", "--production", "-c", _config(tmp_path)]
        )
        assert result.exit_code == 1
        assert "auth.enabled is false" in result.output

    @pytest.mark.parametrize(
        ("blocks", "message"),
        [
            ({"olap": {"grounding": "lenient"}}, "grounding"),
            ({"mcp": {"tools": ["nope"]}}, "unknown tools: nope"),
            (
                {"settings": {"masking": {"enabled": True, "rules": {"email": "hash"}}}},
                "hash_secret",
            ),
        ],
    )
    def test_validate_reports_a_config_that_does_not_load(self, runner, tmp_path, blocks, message):
        result = runner.invoke(cli.main, ["config", "validate", "-c", _config(tmp_path, **blocks)])
        assert result.exit_code == 1
        assert message in result.output

    def test_a_missing_file_is_an_error_not_a_traceback(self, runner, tmp_path):
        result = runner.invoke(cli.main, ["config", "show", "-c", str(tmp_path / "absent.yaml")])
        assert result.exit_code == 1
        assert "absent.yaml" in result.output and "Traceback" not in result.output


class TestServe:
    def test_http_hands_the_app_to_uvicorn(self, runner, tmp_path, monkeypatch):
        import uvicorn

        calls = []
        monkeypatch.setattr(uvicorn, "run", lambda target, **kwargs: calls.append((target, kwargs)))
        for name in ("CONFIG_PATH", "API_HOST", "API_PORT"):
            monkeypatch.setenv(name, "unset")  # restored after the test; serve overwrites them
        config = _config(tmp_path, olap={"enabled": False})
        result = runner.invoke(
            cli.main,
            ["serve", "-c", config, "--host", "127.0.0.1", "--port", "8123"],
            env={"APP_ENV": "production", "API_WORKERS": "3"},
        )
        assert result.exit_code == 0, result.output
        [(target, kwargs)] = calls
        assert target == "trio.main:app"
        assert kwargs == {
            "host": "127.0.0.1",
            "port": 8123,
            "workers": 3,
            "reload": False,
            "access_log": False,
        }

    def test_the_analytical_engine_refuses_more_than_one_worker(
        self, runner, tmp_path, monkeypatch
    ):
        import uvicorn

        calls = []
        monkeypatch.setattr(uvicorn, "run", lambda target, **kwargs: calls.append(kwargs))
        monkeypatch.setenv("CONFIG_PATH", "unset")
        result = runner.invoke(
            cli.main,
            ["serve", "-c", _config(tmp_path)],
            env={"APP_ENV": "production", "API_WORKERS": "2"},
        )
        assert result.exit_code == 1 and "API_WORKERS=2" in result.output and calls == []

    def test_development_reloads_with_one_worker(self, runner, tmp_path, monkeypatch):
        import uvicorn

        calls = []
        monkeypatch.setattr(uvicorn, "run", lambda target, **kwargs: calls.append(kwargs))
        monkeypatch.setenv("CONFIG_PATH", "unset")
        runner.invoke(cli.main, ["serve", "-c", _config(tmp_path)], env={"API_WORKERS": "3"})
        assert calls[0]["reload"] is True and calls[0]["workers"] == 1


class TestServeStdio:
    """stdio builds the OLAP side and runs the MCP server; the fake DB never connects."""

    @pytest.fixture
    def served(self, monkeypatch):
        from tests.e2e.test_one_process import GatewayFactory

        served = {}

        class Server:
            def run(self, transport):
                served["transport"] = transport

        def fake_server(fusion, name, allowed, scope):
            served.update(
                fusion=fusion, allowed=allowed, sources=fusion.sources.source_names(), scope=scope()
            )
            return Server()

        real = cli.build_container
        monkeypatch.setattr(
            cli,
            "build_container",
            lambda settings: real(settings, gateway_factory=GatewayFactory()),
        )
        monkeypatch.setattr("trio.adapters.inbound.mcp.server.create_mcp_server", fake_server)
        monkeypatch.setenv("CONFIG_PATH", "unset")
        return served

    def _run(self, runner, tmp_path, **blocks):
        olap = {"threads": 1, "memory_limit": "256MB"}
        config = _config(tmp_path, olap=olap, **blocks)
        return runner.invoke(cli.main, ["serve", "--transport", "stdio", "-c", config])

    def test_serves_the_configured_tools_and_closes(self, runner, tmp_path, served):
        result = self._run(runner, tmp_path, mcp={"tools": ["query_data"]})
        assert result.exit_code == 0, result.output
        assert served["transport"] == "stdio"
        assert served["allowed"] == frozenset({"query_data"})
        assert served["sources"] == ["shop"]
        assert served["fusion"].sources.source_names() == []  # closed on the way out

    def test_logs_go_to_stderr(self, runner, tmp_path, served):
        import logging

        self._run(runner, tmp_path)
        streams = [h.stream for h in logging.getLogger().handlers if hasattr(h, "stream")]
        assert streams and sys.stdout not in streams

    def _auth(self, **key):
        return {"auth": {"enabled": True, "api_keys": [{"key": "k" * 40, "name": "desk", **key}]}}

    def test_with_auth_the_session_must_act_as_a_key(self, runner, tmp_path, served):
        result = self._run(runner, tmp_path, settings=self._auth(permissions=["query"]))
        assert result.exit_code == 1 and "mcp.stdio_api_key_name" in result.output
        assert served == {}

    def test_acts_as_the_named_key(self, runner, tmp_path, served):
        result = self._run(
            runner,
            tmp_path,
            settings=self._auth(permissions=["query"]),
            mcp={"stdio_api_key_name": "desk"},
        )
        assert result.exit_code == 0, result.output
        assert served["transport"] == "stdio"

    def test_a_key_that_may_not_query_is_refused(self, runner, tmp_path, served):
        result = self._run(
            runner,
            tmp_path,
            settings=self._auth(permissions=["read"]),
            mcp={"stdio_api_key_name": "desk"},
        )
        assert result.exit_code == 1 and "may not run queries" in result.output
        assert served == {}

    def test_the_session_runs_under_its_keys_row_rules(self, runner, tmp_path, served):
        key = {"permissions": ["all"], "row_filters": {"users": [{"column": "id", "value": 1}]}}
        result = self._run(
            runner, tmp_path, settings=self._auth(**key), mcp={"stdio_api_key_name": "desk"}
        )
        assert result.exit_code == 0, result.output
        assert served["scope"].actor == "desk"
        assert served["scope"].conditions_for("users") == (("id", "eq", 1),)

    def test_a_masked_session_is_served_masked(self, runner, tmp_path, served):
        masking = {"enabled": True, "rules": {"email": "redact"}}
        result = self._run(runner, tmp_path, settings={"masking": masking})
        assert result.exit_code == 0, result.output
        scope = served["scope"]
        assert scope.masked
        # No approved catalog, so no labels: refused rather than served raw.
        with pytest.raises(PolicyError, match="no masking labels"):
            scope.masks_for("shop", "users")

    def test_a_catalog_approved_while_serving_goes_live(
        self, runner, tmp_path, served, monkeypatch
    ):
        import time

        from trio.adapters.inbound.http import app as http_app
        from trio.adapters.outbound.catalog_store.file_store import CatalogFileStore
        from trio.application.catalog.catalog_review import CatalogReviewService
        from trio.domain.catalog.catalog import (
            ColumnCatalogEntry,
            DatabaseCatalog,
            TableCatalogEntry,
        )

        monkeypatch.setattr(http_app, "CATALOG_POLL_SECONDS", 0.01)
        seen = {}

        class Server:
            def run(self, transport):
                review = CatalogReviewService(CatalogFileStore(tmp_path / "catalogs"))
                review.save_as_draft(
                    DatabaseCatalog(
                        database_name="shop",
                        tables={
                            "users": TableCatalogEntry(
                                table_name="users",
                                columns=[
                                    ColumnCatalogEntry(
                                        name="email", data_type="varchar", semantic_type="email"
                                    )
                                ],
                            )
                        },
                    )
                )
                review.approve_catalog("shop")
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        seen["masks"] = self.scope.masks_for("shop", "users")
                        return
                    except PolicyError:
                        time.sleep(0.01)

        def fake_server(fusion, name, allowed, scope):
            server = Server()
            server.scope = scope()
            return server

        monkeypatch.setattr("trio.adapters.inbound.mcp.server.create_mcp_server", fake_server)
        settings = {
            "catalog": {"storage_path": str(tmp_path / "catalogs")},
            "masking": {"enabled": True, "rules": {"email": "redact"}},
        }
        result = self._run(runner, tmp_path, settings=settings)
        assert result.exit_code == 0, result.output
        assert seen["masks"] == {"email": "redact"}

    def test_nothing_to_serve_without_olap(self, runner, tmp_path, served):
        config = _config(tmp_path, olap={"enabled": False})
        result = runner.invoke(cli.main, ["serve", "--transport", "stdio", "-c", config])
        assert result.exit_code == 1 and "olap.enabled is false" in result.output
