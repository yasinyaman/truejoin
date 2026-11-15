"""Local users in one JSON file, readable by the server's user only."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Hashable, Iterator
from contextlib import contextmanager
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

from truejoin.domain.governance.users import ManagedUser


class JsonFileUserStore:
    """A ``UserStore`` over one file, replaced atomically on every change."""

    def __init__(self, path: str | Path) -> None:
        """Keep the path; the file is made on the first user."""
        self.path = Path(path)

    def version(self) -> Hashable:
        """The file's modification time and size; None while there is no file."""
        try:
            stat = self.path.stat()
        except FileNotFoundError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    def load(self) -> list[ManagedUser]:
        """Every stored user; none while there is no file."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        return [ManagedUser.from_record(record) for record in data.get("users", [])]

    @contextmanager
    def locked(self) -> Iterator[None]:
        """An exclusive lock on a file beside the store, held for the block.

        shortcut: no lock where fcntl is missing (Windows); one process there,
        or a lock file of another kind, when that is a deployment.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path.with_name(self.path.name + ".lock"), "a") as handle:
            if fcntl is not None:
                fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def save(self, users: list[ManagedUser]) -> None:
        """Write a new file beside the old and rename it over: never half a file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps({"users": [user.as_record() for user in users]}, indent=1)
        fd, temp = tempfile.mkstemp(dir=self.path.parent, prefix=".users-", suffix=".json")
        try:
            # mkstemp makes it 0600: these are password hashes, nobody else needs them.
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, self.path)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise
