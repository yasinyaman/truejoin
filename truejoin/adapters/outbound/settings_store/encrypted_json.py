"""The console's settings in one JSON file; secret values encrypted with a Fernet key."""

from __future__ import annotations

import json
from collections.abc import Hashable
from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path

from cryptography.fernet import InvalidToken

from truejoin.adapters.outbound.database_store.encrypted_json import (
    DatabaseStoreKeyError,
    fernet_of,
    write_private,
)
from truejoin.adapters.outbound.key_store.json_file import JsonFileKeyStore
from truejoin.domain.settings_catalog import is_secret
from truejoin.ports.settings_store import StoredSetting, StoredSettings


class EncryptedJsonSettingsStore:
    """A ``SettingsStore`` over one 0600 file, replaced atomically on every change.

    ``{"settings": {path: {"value": ..., "changed_by": ..., "changed_at": ...}}}``;
    a secret path's value is a Fernet token of its JSON.
    """

    def __init__(self, path: str | Path, key: str | bytes) -> None:
        """Raises: DatabaseStoreKeyError: ``key`` is empty or not a Fernet key."""
        self._fernet = fernet_of(key)
        self.path = Path(path)
        self._file = JsonFileKeyStore(self.path)

    def version(self) -> Hashable:
        return self._file.version()

    def locked(self) -> AbstractContextManager[None]:
        return self._file.locked()

    def load(self) -> StoredSettings:
        """Raises: DatabaseStoreKeyError: A secret does not decrypt with this key."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return StoredSettings()
        values = {}
        for path, record in data.get("settings", {}).items():
            value = record.get("value")
            if is_secret(path) and value:
                try:
                    value = json.loads(self._fernet.decrypt(value.encode()).decode())
                except InvalidToken as e:
                    raise DatabaseStoreKeyError(
                        f"{path} in {self.path} does not decrypt with this key: was it changed?"
                    ) from e
            at = record.get("changed_at")
            values[path] = StoredSetting(
                value=value,
                changed_by=record.get("changed_by"),
                changed_at=datetime.fromisoformat(at) if at else None,
            )
        return StoredSettings(values)

    def save(self, settings: StoredSettings) -> None:
        records = {}
        for path, stored in sorted(settings.values.items()):
            value = stored.value
            if is_secret(path) and value:
                value = self._fernet.encrypt(json.dumps(value).encode()).decode()
            records[path] = {
                "value": value,
                "changed_by": stored.changed_by,
                "changed_at": stored.changed_at.isoformat() if stored.changed_at else None,
            }
        write_private(self.path, json.dumps({"settings": records}, indent=1, default=str))
