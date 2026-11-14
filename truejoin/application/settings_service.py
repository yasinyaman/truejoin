"""The settings an administrator changes from the console, over the configuration file.

The effective settings are the file's, with the console's changes on top for
every path the file does not write and that is not bootstrap
(``domain.settings_catalog``). A change is checked against the whole model
(its validators run), against the production rules, and, for a dangerous
setting, against its typed confirmation, before anything is kept.
"""

from __future__ import annotations

import threading
import types
import typing
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

from truejoin.application.settings import Settings, model_of, validate_production_config
from truejoin.domain.settings_catalog import (
    EXAMPLES,
    group_help_of,
    help_of,
    is_bootstrap,
    is_dangerous,
    is_hidden,
    is_secret,
    section_of,
    split_label,
)
from truejoin.ports.settings_store import SettingsStore, StoredSetting, StoredSettings


class SettingsError(ValueError):
    """A change that cannot be made, with the reason why."""


def model_paths(model: type[BaseModel], prefix: str = "") -> dict[str, Any]:
    """Every leaf setting ``model`` defines, by dotted path, with its annotation."""
    paths: dict[str, Any] = {}
    for name, field in model.model_fields.items():
        if not prefix and name == "databases":
            continue
        inner = model_of(field.annotation)
        if inner is not None:
            paths |= model_paths(inner, f"{prefix}{name}.")
        else:
            paths[f"{prefix}{name}"] = field.annotation
    return paths


def _members(annotation: Any) -> list[Any]:
    if typing.get_origin(annotation) in (typing.Union, types.UnionType):
        return [a for a in typing.get_args(annotation) if a is not type(None)]
    return [annotation]


def kind_of(annotation: Any) -> tuple[str, list[Any]]:
    """How the console edits a value: bool, number, text, choice (with options), list, json."""
    members = _members(annotation)
    first = members[0] if members else annotation
    if typing.get_origin(first) is Literal:
        return "choice", list(typing.get_args(first))
    if first is bool:
        return "bool", []
    if first in (int, float):
        return "number", []
    if first is str:
        return "text", []
    origin = typing.get_origin(first)
    if origin in (list, tuple, set, frozenset):
        return "list", []
    return "json", []


def value_at(settings: Any, path: str) -> Any:
    """The value at a dotted path; None under a missing optional block."""
    value = settings
    for part in path.split("."):
        if value is None:
            return None
        value = getattr(value, part)
    return value.model_dump(mode="json") if isinstance(value, BaseModel) else value


def _put(data: dict[str, Any], path: str, value: Any) -> None:
    *parents, leaf = path.split(".")
    for part in parents:
        child = data.get(part)
        if not isinstance(child, dict):
            child = {}
            data[part] = child
        data = child
    data[leaf] = value


class SettingsService:
    """The console's settings, kept in a store, laid over the file's."""

    def __init__(
        self,
        store: SettingsStore,
        file_settings: Settings,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._store = store
        self._file = file_settings
        self._clock = clock
        self._lock = threading.Lock()
        self._paths = {
            path: annotation
            for path, annotation in model_paths(Settings).items()
            if not is_hidden(path)
        }

    @property
    def file_settings(self) -> Settings:
        return self._file

    def locked_reason(self, path: str) -> str:
        """Why the console may not change ``path``; "" when it may."""
        if is_bootstrap(path):
            return "bootstrap"
        if path in self._file.file_paths:
            return "file"
        return ""

    def stored(self) -> StoredSettings:
        return self._store.load()

    def merged(self, stored: StoredSettings | None = None) -> Settings:
        """The file's settings with the console's on top (locked paths skipped).

        Raises:
            SettingsError: The combination does not validate.
        """
        stored = self.stored() if stored is None else stored
        data = self._file.model_dump()
        for path, setting in stored.values.items():
            if path in self._paths and not self.locked_reason(path):
                _put(data, path, setting.value)
        try:
            merged = Settings.model_validate(data)
        except ValidationError as e:
            raise SettingsError(_readable(e)) from e
        return merged.with_file_paths(self._file.file_paths)

    def effective(self, current: Settings) -> list[dict[str, Any]]:
        """Every setting the console shows, as the server runs with it now."""
        stored = self.stored().values
        defaults = Settings()
        rows = []
        for path, annotation in self._paths.items():
            kind, options = kind_of(annotation)
            secret = is_secret(path)
            value = value_at(current, path)
            source = (
                "file"
                if path in self._file.file_paths
                else "console"
                if path in stored
                else "default"
            )
            entry = stored.get(path)
            group, label = split_label(path)
            rows.append(
                {
                    "path": path,
                    "section": section_of(path),
                    "group": group,
                    "group_help": group_help_of(path),
                    "label": label,
                    "help": help_of(path),
                    "example": EXAMPLES.get(path, ""),
                    "kind": kind,
                    "options": options,
                    "value": None if secret else value,
                    "has_value": bool(value) if secret else None,
                    "default": None if secret else value_at(defaults, path),
                    "source": source,
                    "locked": self.locked_reason(path),
                    "secret": secret,
                    "dangerous": is_dangerous(path),
                    "changed_by": entry.changed_by if entry else None,
                    "changed_at": entry.changed_at.isoformat()
                    if entry and entry.changed_at
                    else None,
                }
            )
        return rows

    def prepare(
        self,
        changes: Mapping[str, Any],
        confirm: Iterable[str],
        by: str | None,
        app_env: str,
        reset: Iterable[str] = (),
    ) -> tuple[Settings, StoredSettings]:
        """The settings after ``changes`` (and ``reset`` paths back to the file or default).

        Nothing is kept: ``commit`` keeps the second value once the server runs with the first.

        Raises:
            SettingsError: An unknown or locked path, a missing confirmation, a value
                that does not validate, or one the production rules refuse.
        """
        confirmed = set(confirm)
        stored = dict(self.stored().values)
        now = self._clock()
        for path in [*changes, *reset]:
            if path not in self._paths:
                raise SettingsError(f"{path} is not a setting the console can change")
            reason = self.locked_reason(path)
            if reason == "bootstrap":
                raise SettingsError(f"{path} is read from the configuration file only")
            if reason == "file":
                raise SettingsError(f"{path} is set in the configuration file, which locks it")
            if is_dangerous(path) and path not in confirmed:
                raise SettingsError(f"{path} can expose data: confirm it by its name")
        for path in reset:
            stored.pop(path, None)
        for path, value in changes.items():
            if is_secret(path) and value in ("", None):
                continue  # a blank secret keeps the one kept
            stored[path] = StoredSetting(value=value, changed_by=by, changed_at=now)
        result = StoredSettings(stored)
        settings = self.merged(result)
        violations = validate_production_config(settings, app_env)
        if violations:
            raise SettingsError("; ".join(violations))
        return settings, result

    def commit(self, stored: StoredSettings) -> StoredSettings:
        """Keep ``stored``; returns what was kept before, to put back if serving fails."""
        with self._lock, self._store.locked():
            before = self._store.load()
            self._store.save(stored)
        return before

    def version(self) -> Any:
        return self._store.version()

    def importable(self, raw: Mapping[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
        """What of the file moves to the console, what stays, and what nothing reads.

        Bootstrap paths stay, and so does a value that reads the environment
        (``${VAR}``, ``${file:...}``): moving it would freeze what it resolves to now.
        """
        moving: dict[str, Any] = {}
        staying: list[str] = []
        unread: list[str] = []
        for path in sorted(self._file.file_paths):
            if is_hidden(path):
                unread.append(path)
            elif path not in self._paths or is_bootstrap(path) or "${" in str(_raw_at(raw, path)):
                staying.append(path)
            else:
                moving[path] = value_at(self._file, path)
        return moving, staying, unread

    def import_values(self, values: Mapping[str, Any], by: str | None) -> StoredSettings:
        """Keep ``values`` as the console's (over what it kept), for ``config import``."""
        now = self._clock()
        with self._lock, self._store.locked():
            kept = dict(self._store.load().values)
            kept |= {
                p: StoredSetting(value=v, changed_by=by, changed_at=now) for p, v in values.items()
            }
            stored = StoredSettings(kept)
            self._store.save(stored)
        return stored


def _readable(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(p) for p in item['loc'])}: {item['msg']}" for item in error.errors()
    )


def _raw_at(raw: Mapping[str, Any], path: str) -> Any:
    value: Any = raw
    for part in path.split("."):
        if not isinstance(value, Mapping):
            return None
        value = value.get(part)
    return value


def pruned(raw: Mapping[str, Any], drop: Iterable[str], prefix: str = "") -> dict[str, Any]:
    """``raw`` (a configuration file's content) without the ``drop`` paths, or emptied blocks."""
    gone = set(drop)
    kept: dict[str, Any] = {}
    for key, value in raw.items():
        path = f"{prefix}{key}"
        if path in gone:
            continue
        if isinstance(value, Mapping) and key != "databases":
            inner = pruned(value, gone, f"{path}.")
            if inner:
                kept[key] = inner
        else:
            kept[key] = value
    return kept
