"""The thin entry points expose the ASGI app and the CLI with the composition root injected."""

import trio.main
from trio.adapters.inbound.cli import main as cli
from trio.adapters.inbound.http.app import runtime_of
from trio.application.settings import RuntimeEnv


def test_asgi_target():
    assert trio.main.app.title == "Warp Engine"
    assert isinstance(runtime_of(trio.main.app).env, RuntimeEnv)


def test_cli_has_container_factory():
    assert cli.catalog.context_settings["obj"]["container_factory"] is cli.container_factory
