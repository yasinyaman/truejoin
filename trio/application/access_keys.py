"""Handing out, listing and revoking API keys, and finding the one a caller presents."""

from __future__ import annotations

import logging
import secrets
import threading
import uuid
from collections.abc import Callable, Hashable, Iterable
from datetime import datetime, timedelta
from typing import Any

from trio.application.settings import ApiKeyConfig, RowRuleConfig
from trio.domain.governance.keys import (
    NAME_RE,
    AccessKeyError,
    Grantor,
    ManagedKey,
    check_grant,
    digest,
    new_secret,
    now,
)
from trio.domain.governance.row_policy import PolicyError, build_policy
from trio.ports.key_store import KeyStore

logger = logging.getLogger(__name__)

#: The longest a key can be made to last; none at all is also allowed.
MAX_DAYS = 3650


class AccessKeyService:
    """The handed-out keys, read again whenever the store says they changed."""

    def __init__(
        self,
        store: KeyStore,
        reserved: Iterable[str] = (),
        clock: Callable[[], datetime] = now,
    ) -> None:
        """``reserved`` are the configured keys' names, which a new key may not take."""
        self._store = store
        self._reserved = {name.lower() for name in reserved}
        self._clock = clock
        self._lock = threading.Lock()
        self._keys: list[ManagedKey] = []
        self._version: Hashable = object()  # matches nothing: the first read loads

    def _fresh(self) -> list[ManagedKey]:
        version = self._store.version()
        if version != self._version:
            try:
                self._keys = self._store.load()
                self._version = version
            except Exception as e:  # a damaged file must not lock everyone out
                logger.error("Could not read the handed-out keys; keeping the last read: %s", e)
        return self._keys

    def now(self) -> datetime:
        """The time a key's expiry is judged by."""
        return self._clock()

    def keys(self) -> list[ManagedKey]:
        """Every handed-out key, revoked ones included, newest first."""
        with self._lock:
            return sorted(self._fresh(), key=lambda k: k.created_at, reverse=True)

    def caller_for(self, secret: str) -> ApiKeyConfig | None:
        """Who a presented key belongs to, if it is a live handed-out key.

        Every stored digest is compared, without stopping at a match, so the
        time taken says nothing about which key (if any) matched.
        """
        candidate = digest(secret)
        when = self._clock()
        with self._lock:
            keys = self._fresh()
        match: ManagedKey | None = None
        for key in keys:
            if secrets.compare_digest(key.digest, candidate):
                match = key
        if match is None or not match.active(when):
            return None
        return as_caller(match)

    def create(  # noqa: PLR0913
        self,
        grantor: Grantor,
        *,
        name: str,
        permissions: list[str],
        tenant: str | None = None,
        roles: list[str] | None = None,
        row_filters: dict[str, list[dict[str, Any]]] | None = None,
        expires_in_days: int | None = None,
    ) -> tuple[ManagedKey, str]:
        """A new key and its secret, which is not kept and cannot be shown again.

        ``roles`` left out means the grantor's own, which are the only ones allowed.

        Raises:
            AccessKeyError: Why it cannot be made.
        """
        roles = list(grantor.roles) if roles is None else list(roles)
        permissions = list(dict.fromkeys(permissions))
        if not NAME_RE.match(name):
            raise AccessKeyError(
                "A key's name is 1 to 64 letters, digits and . _ @ -, starting with a letter "
                "or digit"
            )
        check_grant(grantor, permissions, roles)
        if expires_in_days is not None and not 1 <= expires_in_days <= MAX_DAYS:
            raise AccessKeyError(f"A key lasts 1 to {MAX_DAYS} days, or until it is revoked")
        rules = _row_filters(row_filters or {}, tenant, name)
        when = self._clock()
        secret = new_secret()
        with self._lock, self._store.locked():
            keys = self._reload()
            taken = self._reserved | {k.name.lower() for k in keys}
            if name.lower() in taken:
                # Revoked names stay taken: the audit trail names keys, not ids.
                raise AccessKeyError(f"A key called {name} already exists or did")
            key = ManagedKey(
                id=uuid.uuid4().hex[:12],
                name=name,
                digest=digest(secret),
                hint=secret[-4:],
                permissions=tuple(permissions),
                created_at=when,
                created_by=grantor.name,
                tenant=tenant or None,
                roles=tuple(roles),
                row_filters=rules,
                expires_at=when + timedelta(days=expires_in_days) if expires_in_days else None,
            )
            self._save([*keys, key])
        return key, secret

    def revoke(self, key_id: str, by: str | None) -> ManagedKey:
        """Revoke a key; it stops letting its holder in at once.

        Raises:
            LookupError: No such key.
            AccessKeyError: It was revoked already.
        """
        with self._lock, self._store.locked():
            keys = self._reload()
            found = next((k for k in keys if k.id == key_id), None)
            if found is None:
                raise LookupError(f"No key {key_id}")
            if found.revoked_at is not None:
                raise AccessKeyError(f"{found.name} was revoked already")
            revoked = found.revoke(by, self._clock())
            self._save([revoked if k.id == key_id else k for k in keys])
        return revoked

    def _reload(self) -> list[ManagedKey]:
        """The stored keys as they are now; called under the store's lock."""
        self._version = self._store.version()
        self._keys = self._store.load()
        return self._keys

    def _save(self, keys: list[ManagedKey]) -> None:
        """Write, under the store's lock, so the version read back is this write's."""
        self._store.save(keys)
        self._keys = keys
        self._version = self._store.version()


def as_caller(key: ManagedKey) -> ApiKeyConfig:
    """A handed-out key in the shape the configured keys have."""
    return ApiKeyConfig.model_validate(
        {
            "key": "",  # nothing to compare: the digest matched
            "name": key.name,
            "permissions": list(key.permissions),
            "tenant": key.tenant,
            "roles": list(key.roles),
            "row_filters": key.row_filters,
        }
    )


def _row_filters(
    rules: dict[str, list[dict[str, Any]]], tenant: str | None, name: str
) -> dict[str, list[dict[str, Any]]]:
    """The rules as stored, once they are known to apply to this key's caller."""
    try:
        parsed = {
            table: [RowRuleConfig.model_validate(rule).model_dump() for rule in table_rules]
            for table, table_rules in rules.items()
        }
        policy = build_policy(parsed, caller={"tenant": tenant, "username": name})
        for table in parsed:
            policy.conditions_for(table)
    except (PolicyError, ValueError, TypeError) as e:
        raise AccessKeyError(f"Row rules: {e}") from e
    return {table: table_rules for table, table_rules in parsed.items() if table_rules}
