"""The databases an administrator adds, changes and removes from the console.

This keeps the list and its rules; connecting a database and serving it is
the server's part (``adapters.inbound.http.databases_live``), which connects
first and only then calls ``add`` or ``replace`` here, so a database that
cannot be reached is never kept.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from truejoin.domain.gateway.managed import (
    ManagedDatabase,
    ManagedDatabaseError,
    check_new_name,
)
from truejoin.ports.database_store import DatabaseStore

logger = logging.getLogger(__name__)

#: What a new database must say; the rest have defaults.
REQUIRED = ("name", "type", "database", "username")
#: Every field a request may set.
FIELDS = (
    "name",
    "type",
    "host",
    "port",
    "database",
    "username",
    "password",
    "readonly_username",
    "readonly_password",
    "options",
)


class DatabaseService:
    """The added databases, kept in a store; the configured ones are only names here."""

    def __init__(
        self,
        store: DatabaseStore,
        configured: Iterable[str],
        supported_types: Iterable[str],
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """``configured`` are the YAML databases' names, which an added one may not take."""
        self._store = store
        self._configured = list(configured)
        self._types = sorted(set(supported_types))
        self._clock = clock
        self._lock = threading.Lock()

    @property
    def supported_types(self) -> list[str]:
        return list(self._types)

    def all(self) -> list[ManagedDatabase]:
        return self._store.load()

    def get(self, name: str) -> ManagedDatabase:
        """Raises: LookupError: No database of that name was added here."""
        found = next((d for d in self.all() if d.name == name), None)
        if found is None:
            raise LookupError(f"No database called '{name}' was added from the console")
        return found

    def prepare(self, fields: Mapping[str, Any], by: str | None) -> ManagedDatabase:
        """A new database from a request, checked but not kept.

        Raises:
            ManagedDatabaseError: Why it cannot be added.
        """
        unknown = set(fields) - set(FIELDS)
        if unknown:
            raise ManagedDatabaseError(f"unknown field(s): {', '.join(sorted(unknown))}")
        missing = [f for f in REQUIRED if not fields.get(f)]
        if missing:
            raise ManagedDatabaseError(f"missing: {', '.join(missing)}")
        name = str(fields["name"])
        check_new_name(name, self._taken())
        database = ManagedDatabase.from_record(
            {**fields, "created_at": self._clock().isoformat(), "created_by": by}
        )
        self._check(database)
        return database

    def change(self, name: str, changes: Mapping[str, Any], by: str | None) -> ManagedDatabase:
        """The database ``name`` with ``changes``, checked but not kept.

        Raises:
            LookupError: No such database here.
            ManagedDatabaseError: Why it cannot be changed so.
        """
        unknown = set(changes) - set(FIELDS)
        if unknown:
            raise ManagedDatabaseError(f"unknown field(s): {', '.join(sorted(unknown))}")
        changed = self.get(name).changed(changes, by)
        self._check(changed)
        return changed

    def add(self, database: ManagedDatabase) -> None:
        """Keep a prepared database (its name checked again, under the store's lock)."""
        with self._lock, self._store.locked():
            stored = self._store.load()
            check_new_name(database.name, [*self._configured, *(d.name for d in stored)])
            self._store.save([*stored, database])

    def replace(self, database: ManagedDatabase) -> None:
        """Keep a changed database in place of the one of its name."""
        with self._lock, self._store.locked():
            stored = self._store.load()
            if not any(d.name == database.name for d in stored):
                raise LookupError(
                    f"No database called '{database.name}' was added from the console"
                )
            self._store.save([database if d.name == database.name else d for d in stored])

    def remove(self, name: str) -> ManagedDatabase:
        """Forget a database; returns what was kept.

        Raises:
            LookupError: No such database here.
        """
        with self._lock, self._store.locked():
            stored = self._store.load()
            found = next((d for d in stored if d.name == name), None)
            if found is None:
                raise LookupError(f"No database called '{name}' was added from the console")
            self._store.save([d for d in stored if d.name != name])
        return found

    def is_configured(self, name: str) -> bool:
        return name in self._configured

    def _taken(self) -> list[str]:
        return [*self._configured, *(d.name for d in self.all())]

    def _check(self, database: ManagedDatabase) -> None:
        if database.type not in self._types:
            raise ManagedDatabaseError(
                f"'{database.type}' is not a supported type: {', '.join(self._types)}"
            )
        if not 0 < database.port < 65536:
            raise ManagedDatabaseError("the port is 1 to 65535")
        if not database.database or not database.username:
            raise ManagedDatabaseError("a database and a user name are needed")
        if database.readonly_password and not database.readonly_username:
            raise ManagedDatabaseError("a read-only password needs a read-only user")
