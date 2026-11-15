"""Port: where the local users an administrator makes are kept."""

from collections.abc import Hashable
from contextlib import AbstractContextManager
from typing import Protocol

from truejoin.domain.governance.users import ManagedUser


class UserStore(Protocol):
    """Keeps local users (their password hashes, never the passwords)."""

    def version(self) -> Hashable:
        """Something that changes whenever the stored users do.

        Another process serving the same store may have made, disabled or removed
        a user; a reader compares this before trusting what it last loaded.
        """
        ...

    def load(self) -> list[ManagedUser]:
        """Every stored user, disabled ones included."""
        ...

    def save(self, users: list[ManagedUser]) -> None:
        """Replace the stored users, all at once or not at all."""
        ...

    def locked(self) -> AbstractContextManager[None]:
        """Hold every other process's change off until the block ends."""
        ...
