"""Added databases in one JSON file, their passwords encrypted with a Fernet key."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Hashable
from contextlib import AbstractContextManager
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from truejoin.adapters.outbound.key_store.json_file import JsonFileKeyStore
from truejoin.domain.gateway.managed import SECRET_FIELDS, ManagedDatabase


class DatabaseStoreKeyError(ValueError):
    """The encryption key is missing, malformed, or not the one the file was written with."""


def fernet_of(key: str | bytes) -> Fernet:
    """The cipher for ``key``.

    Raises:
        DatabaseStoreKeyError: ``key`` is empty or not a Fernet key.
    """
    if not key:
        raise DatabaseStoreKeyError("no encryption key: generate one with Fernet.generate_key()")
    try:
        return Fernet(key)
    except (ValueError, TypeError) as e:
        raise DatabaseStoreKeyError(
            "the encryption key is not a Fernet key (32 url-safe base64-encoded bytes)"
        ) from e


def write_private(path: Path, text: str) -> None:
    """Write a new file beside the old and rename it over: never half a file, mode 0600."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem}-", suffix=".json")
    try:
        # mkstemp makes it 0600: hosts, user names and settings are nobody else's business.
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


class EncryptedJsonDatabaseStore:
    """A ``DatabaseStore`` over one 0600 file, replaced atomically on every change.

    Everything but the passwords is plain JSON, so the file can be read and
    backed up like any config; each password is a Fernet token. The key is
    never written anywhere: losing it means entering the passwords again.
    """

    def __init__(self, path: str | Path, key: str | bytes) -> None:
        """Keep the path and the key; the file is made on the first database.

        Raises:
            DatabaseStoreKeyError: ``key`` is empty or not a Fernet key.
        """
        self._fernet = fernet_of(key)
        self.path = Path(path)
        # Same file, version and lock handling as the key store; only the content differs.
        self._file = JsonFileKeyStore(self.path)

    def version(self) -> Hashable:
        return self._file.version()

    def locked(self) -> AbstractContextManager[None]:
        return self._file.locked()

    def load(self) -> list[ManagedDatabase]:
        """Every stored database, passwords decrypted; none while there is no file.

        Raises:
            DatabaseStoreKeyError: A password does not decrypt with this key.
        """
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        databases = []
        for record in data.get("databases", []):
            record = dict(record)
            for secret in SECRET_FIELDS:
                if record.get(secret):
                    try:
                        record[secret] = self._fernet.decrypt(record[secret].encode()).decode()
                    except InvalidToken as e:
                        raise DatabaseStoreKeyError(
                            f"the password of '{record.get('name')}' in {self.path} does not "
                            "decrypt with this key: was the key changed?"
                        ) from e
            databases.append(ManagedDatabase.from_record(record))
        return databases

    def save(self, databases: list[ManagedDatabase]) -> None:
        """Write a new file beside the old and rename it over: never half a file."""
        records = []
        for database in databases:
            record = database.as_record()
            for secret in SECRET_FIELDS:
                if record[secret]:
                    record[secret] = self._fernet.encrypt(record[secret].encode()).decode()
            records.append(record)
        write_private(self.path, json.dumps({"databases": records}, indent=1))
