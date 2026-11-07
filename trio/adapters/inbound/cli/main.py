"""``trio``: the one command tree.

trio serve [--transport http|stdio]
trio catalog {analyze,review,approve,export,list,info,enrich-openapi,pipeline}
trio mcp {convert,info,formats}
trio config {validate,show}
"""

import os
import sys
import threading

import click
import yaml

from trio import __version__
from trio.adapters.inbound.cli import catalog as catalog_cli
from trio.adapters.inbound.cli import mcpgen as mcpgen_cli
from trio.adapters.inbound.http.app import (
    RuntimeContext,
    catalog_versions,
    olap_maskings,
    watch_catalogs,
)
from trio.adapters.inbound.http.auth import AuthenticatedUser, Permission
from trio.adapters.inbound.http.olap import catalogs_approved, load_catalogs, scope_for
from trio.application.container import Container
from trio.application.settings import RuntimeEnv, Settings, masked, validate_production_config
from trio.bootstrap import build_container, load_settings

config_option = click.option(
    "-c",
    "--config",
    "config_path",
    envvar="CONFIG_PATH",
    default="config/database.yaml",
    show_default=True,
    type=click.Path(),
    help="Config file (YAML); also read from CONFIG_PATH",
)


@click.group()
@click.version_option(version=__version__, prog_name="trio")
def main() -> None:
    """Governed REST, OLAP and MCP over your databases, in one process."""


def _load(config_path: str) -> Settings:
    try:
        return load_settings(config_path)
    except (OSError, ValueError, yaml.YAMLError) as e:  # pydantic's error is a ValueError
        raise click.ClickException(f"{config_path}: {e}") from e


def _refuse_unsafe(settings: Settings, env: RuntimeEnv) -> None:
    violations = validate_production_config(settings, env.app_env, list(env.cors_origins))
    for violation in violations:
        click.echo(f"Unsafe production configuration: {violation}", err=True)
    if violations:
        sys.exit(1)


# -- serve ----------------------------------------------------------------------


@main.command()
@click.option(
    "--transport",
    type=click.Choice(["http", "stdio"]),
    default="http",
    show_default=True,
    help="http: the REST gateway, /olap and MCP on one port. stdio: the MCP tools only.",
)
@config_option
@click.option("--host", default=None, help="Bind host (default: API_HOST, else 0.0.0.0)")
@click.option("--port", default=None, type=int, help="Bind port (default: API_PORT, else 8000)")
def serve(transport: str, config_path: str, host: str | None, port: int | None) -> None:
    """Run the server."""
    # The app module and any worker process read these from the environment.
    os.environ["CONFIG_PATH"] = config_path
    if host:
        os.environ["API_HOST"] = host
    if port:
        os.environ["API_PORT"] = str(port)
    env = RuntimeEnv.from_environ()
    if transport == "stdio":
        _serve_stdio(config_path, env)
        return

    import uvicorn

    reload = env.app_env == "development"
    workers = env.api_workers if not reload else 1
    if workers > 1 and _load(config_path).olap.enabled:
        # Each worker would build its own DuckDB, views, cache and rate limits.
        raise click.ClickException(
            f"API_WORKERS={workers}: the analytical engine lives in one process, so "
            f"/olap and MCP need API_WORKERS=1 (or olap.enabled: false to run the "
            f"gateway alone on several workers)."
        )
    uvicorn.run(
        "trio.main:app",
        host=env.api_host,
        port=env.api_port,
        workers=workers,
        reload=reload,
        access_log=not env.is_production,
    )


def _stdio_user(settings: Settings) -> AuthenticatedUser | None:
    """Who a stdio session is: the key named by ``mcp.stdio_api_key_name``.

    There is no request to authenticate on stdio, so with auth enabled the
    session acts as one configured key and gets exactly what that key may do,
    row rules and masks included.
    """
    auth = settings.settings.auth
    if not auth.enabled:
        return None
    name = settings.mcp.stdio_api_key_name
    key = next((k for k in auth.api_keys if k.name == name), None)
    if key is None:
        raise click.ClickException(
            "auth is enabled: set mcp.stdio_api_key_name to the name of the API key "
            "the stdio session acts as" + (f" ('{name}' is not configured)" if name else "")
        )
    user = AuthenticatedUser(key)
    if not user.grants(Permission.QUERY):
        raise click.ClickException(
            f"API key '{name}' may not run queries (it needs the 'query' permission)"
        )
    return user


def _serve_stdio(config_path: str, env: RuntimeEnv) -> None:
    from trio.adapters.inbound.mcp.server import create_mcp_server
    from trio.observability.warp_logging import setup_logging

    # stdout carries the MCP protocol; every log line must go to stderr.
    setup_logging(level=env.log_level, json_format=env.log_json, stream=sys.stderr)
    settings = _load(config_path)
    _refuse_unsafe(settings, env)
    user = _stdio_user(settings)
    container = build_container(settings)
    if container.olap_factory is None:
        raise click.ClickException("olap.enabled is false: there is nothing to serve over stdio")
    fusion = container.olap_factory(env.app_env)
    stop = threading.Event()
    try:
        # What the HTTP server keeps, so a catalog approved later goes live here too.
        runtime = RuntimeContext(
            env=env,
            container=container,
            settings=settings,
            fusion=fusion,
            maskings=olap_maskings(container),
            seen_catalogs=catalog_versions(container),
        )
        scope = scope_for(user, settings.settings.masking, runtime.maskings)
        fusion.tools.readiness_checks.append(
            lambda: catalogs_approved(fusion, container.repository)
        )
        load_catalogs(fusion, container.repository, terms=settings.olap.terms == "on")
        threading.Thread(
            target=watch_catalogs, args=(runtime, stop), name="catalog-watch", daemon=True
        ).start()
        server = create_mcp_server(
            fusion, name="trio", allowed=settings.offered_tools(), scope=lambda: scope
        )
        server.run(transport="stdio")
    finally:
        stop.set()
        fusion.close()


# -- catalog (Warp's commands, as they are) ------------------------------------------


def container_factory(config_path: str) -> Container:
    """Load the config at `config_path` and wire the default adapters."""
    return build_container(load_settings(config_path))


catalog = catalog_cli.main
catalog.context_settings = {
    **catalog.context_settings,
    "obj": {"container_factory": container_factory},
}
main.add_command(catalog, "catalog")


# -- mcp (Stargate's converter) --------------------------------------------------------


@main.group()
def mcp() -> None:
    """Turn an OpenAPI or Postman spec into an MCP server definition."""


for _name in ("convert", "info", "formats"):
    mcp.add_command(mcpgen_cli.main.commands[_name])


# -- config -----------------------------------------------------------------------------


@main.group()
def config() -> None:
    """Check or print the configuration."""


@config.command()
@config_option
@click.option(
    "--production", is_flag=True, help="Apply the production safety rules whatever APP_ENV says"
)
def validate(config_path: str, production: bool) -> None:
    """Load the config (secrets files included) and apply the startup checks."""
    env = RuntimeEnv.from_environ()
    if production:
        env = RuntimeEnv(app_env="production", cors_origins=env.cors_origins)
    settings = _load(config_path)
    _refuse_unsafe(settings, env)
    click.echo(
        f"{config_path}: valid ({len(settings.databases)} database(s), APP_ENV={env.app_env})"
    )


@config.command()
@config_option
def show(config_path: str) -> None:
    """Print the effective configuration with every secret masked."""
    dumped = masked(_load(config_path).model_dump(mode="json"))
    click.echo(yaml.safe_dump(dumped, sort_keys=False, allow_unicode=True), nl=False)
