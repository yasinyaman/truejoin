"""Handed-out keys in one JSON file, readable by the server's user only."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Hashable
from pathlib import Path

from trio.domain.governance.keys import ManagedKey


class JsonFileKeyStore:
    """A ``KeyStore`` over one file, replaced atomically on every change."""

    def __init__(self, path: str | Path) -> None:
        """Keep the path; the file is made on the first key."""
        self.path = Path(path)

    def version(self) -> Hashable:
        """The file's modification time and size; None while there is no file."""
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def load(self) -> list[ManagedKey]:
        """Every stored key; none while there is no file."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        return [ManagedKey.from_record(record) for record in data.get("keys", [])]

    def save(self, keys: list[ManagedKey]) -> None:
        """Write a new file beside the old and rename it over: never half a file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps({"keys": [key.as_record() for key in keys]}, indent=1)
        fd, temp = tempfile.mkstemp(dir=self.path.parent, prefix=".keys-", suffix=".json")
        try:
            # mkstemp makes it 0600: digests are not keys, but nobody else needs them.
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, self.path)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise
