"""``truejoin users``: make and look after local users from a shell.

The console manages users too, but someone has to be the first administrator
(and to get back in when every administrator's password is lost). These commands
work on the user file directly, so they are for whoever can run truejoin
and read its configuration: that person already holds every permission.
"""

from __future__ import annotations

import click

from truejoin.application.local_users import LocalUsers
from truejoin.bootstrap import build_local_users, load_settings
from truejoin.domain.governance.keys import PERMISSIONS, Grantor
from truejoin.domain.governance.users import UserError

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


def _service(config_path: str) -> LocalUsers:
    try:
        settings = load_settings(config_path)
    except (OSError, ValueError) as e:
        raise click.ClickException(f"{config_path}: {e}") from e
    service = build_local_users(settings)
    if service is None:
        raise click.ClickException(
            f"{config_path} has no settings.auth.users: set its store to a file for the users."
        )
    return service


def _operator(roles: tuple[str, ...] = ()) -> Grantor:
    """The person at the shell: everything, as whoever's roles the account has."""
    return Grantor(name="cli", permissions=("all",), roles=roles, has_row_rules=False)


@click.group()
def users() -> None:
    """Local users: list, add, set a password, disable."""


@users.command("list")
@config_option
def list_users(config_path: str) -> None:
    """Every local user, with what they may do."""
    rows = _service(config_path).users()
    if not rows:
        click.echo(
            "No local users yet. Make the first with: truejoin users add NAME --permission all"
        )
        return
    for user in rows:
        state = "disabled" if user.disabled else "active"
        last = user.last_sign_in.date().isoformat() if user.last_sign_in else "never"
        click.echo(
            f"{user.name:<24} {state:<9} {','.join(user.permissions):<28} last sign-in {last}"
        )


@users.command()
@click.argument("name")
@click.option(
    "--permission",
    "permissions",
    multiple=True,
    type=click.Choice(PERMISSIONS),
    default=("all",),
    show_default=True,
    help="What the user may do; repeat for several",
)
@click.option("--display", help="The name shown beside theirs")
@click.option("--tenant", help="What ${tenant} resolves to")
@click.password_option(
    "--password", prompt="Password", confirmation_prompt=True, help="Asked for if left out"
)
@config_option
def add(
    name: str,
    permissions: tuple[str, ...],
    display: str | None,
    tenant: str | None,
    password: str,
    config_path: str,
) -> None:
    """Make a user (the first one is usually an administrator: --permission all)."""
    service = _service(config_path)
    try:
        user = service.create(
            _operator(),
            name=name,
            password=password,
            permissions=list(permissions),
            display=display,
            tenant=tenant,
        )
    except UserError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"Made {user.name} ({', '.join(user.permissions)}).")


@users.command()
@click.argument("name")
@click.password_option(
    "--password", prompt="New password", confirmation_prompt=True, help="Asked for if left out"
)
@config_option
def passwd(name: str, password: str, config_path: str) -> None:
    """Set a user's password (it ends whatever they were signed in with)."""
    service = _service(config_path)
    target = service.get(name)
    if target is None:
        raise click.ClickException(f"No user {name}")
    try:
        service.set_password(_operator(target.roles), target.name, password)
    except UserError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"Password of {target.name} changed.")


def _set_disabled(name: str, disabled: bool, config_path: str) -> None:
    service = _service(config_path)
    target = service.get(name)
    if target is None:
        raise click.ClickException(f"No user {name}")
    try:
        service.update(_operator(target.roles), target.name, disabled=disabled)
    except UserError as e:
        raise click.ClickException(str(e)) from e
    click.echo(f"{target.name} is {'disabled' if disabled else 'enabled'}.")


@users.command()
@click.argument("name")
@config_option
def disable(name: str, config_path: str) -> None:
    """Stop a user signing in; what they were signed in with stops at once."""
    _set_disabled(name, True, config_path)


@users.command()
@click.argument("name")
@config_option
def enable(name: str, config_path: str) -> None:
    """Let a disabled user sign in again."""
    _set_disabled(name, False, config_path)
