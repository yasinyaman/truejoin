"""``truejoin``: the one command tree.

truejoin serve [--transport http|stdio]
truejoin catalog {analyze,review,approve,export,list,info,enrich-openapi,pipeline}
truejoin mcp {convert,info,formats}
truejoin config {validate,show}
truejoin users {list,add,passwd,disable,enable}
truejoin pilot {run,compare}
"""

import hashlib
import os
import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import click
import yaml

from truejoin import __version__
from truejoin.adapters.inbound.cli import catalog as catalog_cli
from truejoin.adapters.inbound.cli import mcpgen as mcpgen_cli
from truejoin.adapters.inbound.cli.users import users as users_cli
from truejoin.adapters.inbound.http.app import (
    RuntimeContext,
    catalog_versions,
    olap_maskings,
    watch_catalogs,
)
from truejoin.adapters.inbound.http.auth import AuthenticatedUser, Permission
from truejoin.adapters.inbound.http.olap import catalogs_approved, load_catalogs, scope_for
from truejoin.application.container import Container
from truejoin.application.olap.app import FusionApp
from truejoin.application.olap.pilot import run_pilot
from truejoin.application.settings import RuntimeEnv, Settings, masked, validate_production_config
from truejoin.application.settings_service import pruned
from truejoin.bootstrap import build_container, load_settings, store_key_problems
from truejoin.domain.governance.access import AccessScope
from truejoin.domain.olap.pilot import parse_questions, render_comparison, render_report, tally

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
@click.version_option(version=__version__, prog_name="truejoin")
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
        "truejoin.main:app",
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
    return _key_user(
        settings,
        settings.mcp.stdio_api_key_name,
        "auth is enabled: set mcp.stdio_api_key_name to the name of the API key "
        "the stdio session acts as",
    )


def _key_user(settings: Settings, name: str | None, missing: str) -> AuthenticatedUser | None:
    """The configured key called ``name``, as a caller; None while auth is off."""
    auth = settings.settings.auth
    if not auth.enabled:
        return None
    key = next((k for k in auth.api_keys if k.name == name), None)
    if key is None:
        raise click.ClickException(missing + (f" ('{name}' is not configured)" if name else ""))
    user = AuthenticatedUser(key)
    if not user.grants(Permission.QUERY):
        raise click.ClickException(
            f"API key '{name}' may not run queries (it needs the 'query' permission)"
        )
    return user


def _serve_stdio(config_path: str, env: RuntimeEnv) -> None:
    from truejoin.adapters.inbound.mcp.server import create_mcp_server
    from truejoin.observability.warp_logging import setup_logging

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
        fusion.start_refresh()
        threading.Thread(
            target=watch_catalogs, args=(runtime, stop), name="catalog-watch", daemon=True
        ).start()
        server = create_mcp_server(
            fusion, name="truejoin", allowed=settings.offered_tools(), scope=lambda: scope
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
main.add_command(users_cli, "users")


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
    problems = store_key_problems(settings)
    for problem in problems:
        click.echo(f"Cannot start: {problem}", err=True)
    if problems:
        sys.exit(1)
    click.echo(
        f"{config_path}: valid ({len(settings.databases)} database(s), APP_ENV={env.app_env})"
    )


@config.command()
@config_option
def show(config_path: str) -> None:
    """Print the effective configuration with every secret masked."""
    dumped = masked(_load(config_path).model_dump(mode="json"))
    click.echo(yaml.safe_dump(dumped, sort_keys=False, allow_unicode=True), nl=False)


@config.command(name="import")
@config_option
@click.option(
    "--minimal-out",
    type=click.Path(dir_okay=False),
    help="Write the file with only what must stay in it (comments are not kept)",
)
def import_settings(config_path: str, minimal_out: str | None) -> None:
    """Move the file's settings to the console's store; the file keeps only what it must.

    Bootstrap settings and values read from the environment (${VAR}) stay in
    the file. Until a moved setting is deleted from the file, the file locks it.
    """
    settings = _load(config_path)
    container = build_container(settings)
    service = container.settings_service
    if service is None:
        raise click.ClickException("settings.settings_store.path is not set: there is no store")
    raw = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    moving, staying, unread = service.importable(raw)
    if not moving:
        click.echo("Nothing to move: the file holds only what must stay in it.")
        return
    service.import_values(moving, by="config import")
    click.echo(f"Moved {len(moving)} setting(s) to the store; {len(staying)} stay in the file.")
    if minimal_out:
        minimal = pruned(raw, [*moving, *unread])  # nothing reads the unread ones
        Path(minimal_out).write_text(
            yaml.safe_dump(minimal, sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        click.echo(f"Wrote {minimal_out}: serve with it and the moved settings are the console's.")
    else:
        click.echo("Delete these from the file, or the file keeps them locked:")
        for path in sorted(moving):
            click.echo(f"  {path}")


# -- pilot ------------------------------------------------------------------------------


@main.group()
def pilot() -> None:
    """Run a customer's own questions and report each as correct, refused or wrong."""


@contextmanager
def _engine(settings: Settings) -> Iterator[tuple[FusionApp, Container]]:
    """The analytical engine with its catalogs, as ``serve`` builds it, closed on the way out."""
    if settings.olap.terms != "on":
        raise click.ClickException("olap.terms is off: questions in words need the term layer")
    container = build_container(settings)
    if container.olap_factory is None:
        raise click.ClickException("olap.enabled is false: there is no engine to ask")
    fusion = container.olap_factory(RuntimeEnv.from_environ().app_env)
    try:
        load_catalogs(fusion, container.repository, terms=True)
        yield fusion, container
    finally:
        fusion.close()


def _scope(settings: Settings, container: Container, name: str | None) -> AccessScope:
    """What the key called ``name`` may see; with auth off, what any caller sees."""
    if name and not settings.settings.auth.enabled:
        raise click.ClickException(f"auth is disabled: there is no key '{name}' to act as")
    user = _key_user(settings, name, "auth is enabled: name the API key to act as with --as")
    return scope_for(user, settings.settings.masking, olap_maskings(container))


def _applies(scope: AccessScope) -> str:
    rules = "row rules apply" if not scope.policy.is_empty else "no row rules"
    return f"{rules}; {'columns masked' if scope.masked else 'nothing masked'}"


as_option = click.option(
    "--as",
    "key_name",
    default=None,
    help="The API key to act as: its row rules and masks apply (required with auth on).",
)


@pilot.command("run")
@click.argument("questions_path", type=click.Path(exists=True, dir_okay=False))
@config_option
@as_option
@click.option(
    "--out",
    default="pilot-report.md",
    show_default=True,
    type=click.Path(dir_okay=False),
    help="Where to write the report",
)
@click.option(
    "--with-questions",
    is_flag=True,
    help="Put the question text and refusal messages in the report (then keep it inside).",
)
@click.option("--max-wrong", default=2, show_default=True, help="Wrong answers the pilot allows")
def pilot_run(
    questions_path: str,
    config_path: str,
    key_name: str | None,
    out: str,
    with_questions: bool,
    max_wrong: int,
) -> None:
    """Answer every question in QUESTIONS_PATH (YAML) and write the pilot report."""
    raw = Path(questions_path).read_bytes()
    try:
        questions = parse_questions(yaml.safe_load(raw))
    except (ValueError, yaml.YAMLError) as e:
        raise click.ClickException(f"{questions_path}: {e}") from e
    settings = _load(config_path)
    with _engine(settings) as (fusion, container):
        scope = _scope(settings, container, key_name)
        outcomes = run_pilot(fusion.tools, fusion.query, questions, scope)
    about = [
        ("Run", datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")),
        ("Version", f"truejoin {__version__}"),
        ("As", f"{key_name or 'no key (auth off)'}: {_applies(scope)}"),
        (
            "Questions file",
            f"{len(questions)} questions, sha256 {hashlib.sha256(raw).hexdigest()[:16]}",
        ),
        ("Databases", ", ".join(fusion.sources.source_names()) or "none"),
    ]
    Path(out).write_text(
        render_report(questions, outcomes, about, max_wrong, with_questions), encoding="utf-8"
    )
    counts = tally(outcomes)
    click.echo(
        f"{out}: {counts['correct']} correct, {counts['refused']} refused, "
        f"{counts['wrong']} wrong, {counts['unchecked']} not checked"
    )


@pilot.command("compare")
@click.argument("question")
@config_option
@click.option(
    "--as", "key_names", multiple=True, required=True, help="An API key to act as; repeat it"
)
@click.option("--rows", "limit", default=10, show_default=True, help="Rows to show per caller")
def pilot_compare(question: str, config_path: str, key_names: tuple[str, ...], limit: int) -> None:
    """Ask QUESTION as each key and print the answers side by side (row values included)."""
    settings = _load(config_path)
    with _engine(settings) as (fusion, container):
        answers = []
        for name in key_names:
            scope = _scope(settings, container, name)
            answer = fusion.tools.for_scope(scope).execute(
                "answer_question", {"question": question}
            )
            answers.append((name, _applies(scope), answer))
    click.echo(render_comparison(question, answers, limit))
