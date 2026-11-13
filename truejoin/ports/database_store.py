"""Port: where the databases an administrator adds from the console are kept."""

from collections.abc import Hashable
from contextlib import AbstractContextManager
from typing import Protocol

from truejoin.domain.gateway.managed import ManagedDatabase


class DatabaseStore(Protocol):
    """Keeps added databases; their passwords are protected at rest by the store."""

    def version(self) -> Hashable:
        """Something that changes whenever the stored databases do."""
        ...

    def load(self) -> list[ManagedDatabase]:
        """Every stored database, passwords readable."""
        ...

    def save(self, databases: list[ManagedDatabase]) -> None:
        """Replace the stored databases, all at once or not at all."""
        ...

    def locked(self) -> AbstractContextManager[None]:
        """Hold every other process's change off until the block ends."""
        ...
