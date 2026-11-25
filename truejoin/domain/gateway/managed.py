"""Databases an administrator adds from the console, beside the configured ones.

A configured database lives in the YAML file and is read once; one added from
the console is kept in a store (its passwords encrypted there) and connected
at once, without a restart. Its name is the OLAP schema, the URL segment
(``{api_prefix}/{name}``) and the MCP source name, so it is held to the rules
all three can take.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

from truejoin.domain.gateway.connection_errors import classify

#: A SQL schema name and a URL segment both: no dots, no dashes.
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")
#: Taken by the server's own routes under the API prefix.
RESERVED = frozenset({"catalog", "audit", "keys", "databases", "settings", "auth"})
#: The fields that hold a secret: never returned, never logged.
SECRET_FIELDS = ("password", "readonly_password")
#: A type's usual port, for a database added without one.
DEFAULT_PORTS = {
    "postgresql": 5432,
    "postgres": 5432,
    "mysql": 3306,
    "mariadb": 3306,
    "mssql": 1433,
    "sqlserver": 1433,
    "oracle": 1521,
}


class ManagedDatabaseError(ValueError):
    """A database that cannot be added, changed or removed, with the reason why.

    ``reason`` is one of ``connection_errors.REASONS`` when connecting failed
    (empty otherwise), and ``raw`` the driver's own words, passwords scrubbed.
    """

    def __init__(self, message: str = "", reason: str = "", raw: str = "") -> None:
        super().__init__(message)
        self.reason = reason
        self.raw = raw


def explain_failure(
    error: BaseException,
    doing: str,
    *,
    host: str = "",
    port: int | None = None,
    database: str = "",
    secrets: Iterable[str] = (),
    text: str = "",
) -> ManagedDatabaseError:
    """``error``, raised connecting, as advice first and the driver's words after.

    ``text`` is the driver's message when ``error`` only wraps it (a retry
    that gave up keeps the last error as text).
    """
    raw = scrub(text or str(error) or type(error).__name__, secrets)
    problem = classify(error, host, port, database)
    if problem.reason == "unknown" and text:
        problem = classify(Exception(text), host, port, database)
    message = f"{doing}: {problem.advice} ({raw})" if problem.advice else f"{doing}: {raw}"
    return ManagedDatabaseError(message, reason=problem.reason, raw=raw)


@dataclass(frozen=True, slots=True)
class ManagedDatabase:
    """One database added from the console, as kept: its passwords in clear here."""

    name: str
    type: str
    database: str
    username: str
    host: str = "localhost"
    port: int = 5432
    password: str = ""
    readonly_username: str = ""
    readonly_password: str = ""
    options: Mapping[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    created_by: str | None = None
    updated_at: datetime | None = None
    updated_by: str | None = None

    def connection(self) -> dict[str, Any]:
        """What a ``DatabaseConfig`` is made of: the connection, no bookkeeping."""
        return {
            "name": self.name,
            "type": self.type,
            "host": self.host,
            "port": self.port,
            "database": self.database,
            "username": self.username,
            "password": self.password,
            "readonly_username": self.readonly_username,
            "readonly_password": self.readonly_password,
            "options": dict(self.options),
        }

    def public(self) -> dict[str, Any]:
        """What may be shown: everything but the passwords, which become yes/no."""
        record = self.as_record()
        for secret in SECRET_FIELDS:
            record[f"has_{secret}"] = bool(record.pop(secret))
        return record

    def as_record(self) -> dict[str, Any]:
        """The stored form (a store encrypts the secrets before writing it)."""
        record = self.connection()
        for key in ("created_at", "updated_at"):
            value = getattr(self, key)
            record[key] = value.isoformat() if value else None
        record["created_by"] = self.created_by
        record["updated_by"] = self.updated_by
        return record

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> ManagedDatabase:
        def when(key: str) -> datetime | None:
            value = record.get(key)
            return datetime.fromisoformat(value) if value else None

        return cls(
            name=str(record["name"]),
            type=str(record["type"]),
            host=str(record.get("host") or "localhost"),
            port=int(record.get("port") or DEFAULT_PORTS.get(str(record["type"]), 5432)),
            database=str(record.get("database") or ""),
            username=str(record.get("username") or ""),
            password=str(record.get("password") or ""),
            readonly_username=str(record.get("readonly_username") or ""),
            readonly_password=str(record.get("readonly_password") or ""),
            options=dict(record.get("options") or {}),
            created_at=when("created_at"),
            created_by=record.get("created_by"),
            updated_at=when("updated_at"),
            updated_by=record.get("updated_by"),
        )

    def changed(self, changes: Mapping[str, Any], by: str | None) -> ManagedDatabase:
        """This database with ``changes``; a blank password keeps the one it has."""
        unknown = set(changes) - set(self.connection()) - {"name"}
        if unknown:
            raise ManagedDatabaseError(f"cannot change {', '.join(sorted(unknown))}")
        if changes.get("name", self.name) != self.name:
            raise ManagedDatabaseError("a database cannot be renamed: remove it and add it again")
        kept = {k: v for k, v in changes.items() if not (k in SECRET_FIELDS and not v)}
        kept.pop("name", None)
        return replace(self, **kept, updated_at=datetime.now(UTC), updated_by=by)


def check_new_name(name: str, taken: Iterable[str]) -> None:
    """Refuse a name that is malformed, reserved, or already a database's.

    Raises:
        ManagedDatabaseError: Naming what is wrong with it.
    """
    if not NAME_RE.match(name):
        raise ManagedDatabaseError(
            f"'{name}' cannot be a database name: letters, digits and underscores, "
            "starting with a letter or underscore, at most 63"
        )
    if name.lower() in RESERVED:
        raise ManagedDatabaseError(f"'{name}' is the name of one of the server's own routes")
    if name.lower() in {t.lower() for t in taken}:
        raise ManagedDatabaseError(f"a database called '{name}' already exists")


def scrub(message: str, secrets: Iterable[str]) -> str:
    """A driver's message with the passwords it may echo taken out."""
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        message = message.replace(secret, "***")
    return re.sub(r"(?i)(PWD|PASSWORD)=[^;\s]*", r"\1=***", message)
