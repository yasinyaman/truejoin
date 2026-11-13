"""Port: where the settings changed from the console are kept."""

from collections.abc import Hashable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol


@dataclass(frozen=True)
class StoredSetting:
    """One setting the console changed: its value (in clear here) and who changed it when."""

    value: Any
    changed_by: str | None = None
    changed_at: datetime | None = None


@dataclass(frozen=True)
class StoredSettings:
    """Everything the console changed, by dotted path."""

    values: dict[str, StoredSetting] = field(default_factory=dict)


class SettingsStore(Protocol):
    """Keeps the console's settings; secret values are protected at rest by the store."""

    def version(self) -> Hashable:
        """Something that changes whenever the stored settings do."""
        ...

    def load(self) -> StoredSettings: ...

    def save(self, settings: StoredSettings) -> None:
        """Replace them, all at once or not at all."""
        ...

    def locked(self) -> AbstractContextManager[None]: ...
