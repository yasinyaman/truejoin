"""Port: where the keys an administrator hands out are kept."""

from collections.abc import Hashable
from typing import Protocol

from trio.domain.governance.keys import ManagedKey


class KeyStore(Protocol):
    """Keeps handed-out keys (their digests, never the keys themselves)."""

    def version(self) -> Hashable:
        """Something that changes whenever the stored keys do.

        Another process serving the same store may have handed out or revoked
        a key; a reader compares this before trusting what it last loaded.
        """
        ...

    def load(self) -> list[ManagedKey]:
        """Every stored key, revoked ones included."""
        ...

    def save(self, keys: list[ManagedKey]) -> None:
        """Replace the stored keys, all at once or not at all."""
        ...
