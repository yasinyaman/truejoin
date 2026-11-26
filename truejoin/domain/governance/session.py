"""Who a statement runs for, as the database itself is told.

Truejoin applies its own row rules and masks. A DBA may also have rules in the
database: a SQL Server security policy reading ``SESSION_CONTEXT``, or an
Oracle VPD policy reading the client identifier or an application context. For
those to apply, each statement's session has to say who it runs for. A
database's ``options.session_identity`` asks for that; :class:`SessionIdentity`
is what is said, and :data:`CURRENT_IDENTITY` carries it from the sign-in to
the adapter that opens the session.

Only the REST paths run as their caller (``AuthManager`` sets it). Everything
that loads data shared by several callers runs as the service, with no caller:
the OLAP side (its slices serve every caller that asks the same thing), catalog
analysis and history mining (a sample filtered by one person's rules would
describe their rows, not the table), discovery and refresh. :func:`as_service`
says so where a caller is known but must not be used.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

#: Callers' fields a database may be told, by the name a config uses for them.
FIELDS = ("actor", "tenant", "roles")

_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
#: A procedure name is written into an anonymous block, so it is held to this.
_PROCEDURE = re.compile(r"^[A-Za-z][A-Za-z0-9_$#]{0,127}(\.[A-Za-z][A-Za-z0-9_$#]{0,127}){0,2}$")

_SQL_SERVER = frozenset({"mssql", "sqlserver"})
_ORACLE = frozenset({"oracle"})


@dataclass(frozen=True, slots=True)
class SessionIdentity:
    """The caller a statement runs for: their name, tenant and roles."""

    actor: str
    tenant: str | None = None
    roles: tuple[str, ...] = ()

    def value(self, field: str) -> str | None:
        """One field as the database receives it (roles joined by commas)."""
        if field == "actor":
            return self.actor
        if field == "tenant":
            return self.tenant
        if field == "roles":
            return ",".join(self.roles) if self.roles else None
        raise ValueError(f"unknown session field '{field}'")


CURRENT_IDENTITY: ContextVar[SessionIdentity | None] = ContextVar(
    "truejoin_session_identity", default=None
)


def current_identity() -> SessionIdentity | None:
    """The caller the running statement is for, or None for the service."""
    return CURRENT_IDENTITY.get()


@contextmanager
def as_service() -> Iterator[None]:
    """Run the block with no caller: what it loads is the service's, shared by all."""
    token = CURRENT_IDENTITY.set(None)
    try:
        yield
    finally:
        CURRENT_IDENTITY.reset(token)


@dataclass(frozen=True, slots=True)
class SessionIdentityConfig:
    """What one database is told about each caller (``options.session_identity``).

    - ``session_context`` (SQL Server): field -> ``SESSION_CONTEXT`` key, set
      with ``sp_set_session_context`` on every statement's session.
    - ``client_identifier`` (Oracle): the field sent as the client identifier
      (``SYS_CONTEXT('USERENV', 'CLIENT_IDENTIFIER')``).
    - ``context_procedure`` (Oracle): a procedure called with
      ``(actor, tenant, roles)`` to set an application context; ``CREATE
      CONTEXT ... USING`` lets only that procedure set it.

    With no caller every value is NULL, so a session never keeps the previous
    caller's identity.
    """

    session_context: tuple[tuple[str, str], ...] = ()
    client_identifier: str | None = None
    context_procedure: str | None = None


def parse_session_identity(
    db_type: str, options: Mapping[str, Any]
) -> SessionIdentityConfig | None:
    """``options.session_identity`` checked against what ``db_type`` can do.

    Raises:
        ValueError: On an unknown key or field, a key or procedure name that is
            not a plain identifier, or a setting the engine has no use for.
    """
    raw = options.get("session_identity")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise ValueError("options.session_identity must be a mapping")
    kind = db_type.lower()
    unknown = set(raw) - {"session_context", "client_identifier", "context_procedure"}
    if unknown:
        raise ValueError(f"options.session_identity: unknown setting(s) {sorted(unknown)}")

    pairs: list[tuple[str, str]] = []
    context = raw.get("session_context")
    if context is not None:
        if kind not in _SQL_SERVER:
            raise ValueError("session_identity.session_context is SQL Server's (type mssql)")
        if not isinstance(context, Mapping) or not context:
            raise ValueError("session_identity.session_context maps fields to SESSION_CONTEXT keys")
        for field, key in context.items():
            _field(field)
            if not isinstance(key, str) or not _KEY.match(key):
                raise ValueError(
                    f"session_identity.session_context: '{key}' is not a plain key name"
                )
            pairs.append((str(field), key))

    identifier = raw.get("client_identifier")
    procedure = raw.get("context_procedure")
    if (identifier is not None or procedure is not None) and kind not in _ORACLE:
        raise ValueError(
            "session_identity.client_identifier and context_procedure are Oracle's (type oracle)"
        )
    if identifier is not None:
        _field(identifier)
    if procedure is not None and (
        not isinstance(procedure, str) or not _PROCEDURE.match(procedure)
    ):
        raise ValueError(
            f"session_identity.context_procedure: '{procedure}' is not a procedure name "
            "(schema.package.procedure at most)"
        )
    config = SessionIdentityConfig(tuple(pairs), identifier, procedure)
    if config == SessionIdentityConfig():
        raise ValueError("options.session_identity says nothing to send")
    return config


def _field(name: Any) -> None:
    if name not in FIELDS:
        raise ValueError(f"session_identity: '{name}' is not one of {', '.join(FIELDS)}")
