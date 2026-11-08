"""API keys an administrator hands out from the console, beside the configured ones.

A key is shown once, when it is made, and kept only as its SHA-256 digest:
like the configured keys it is a random token of 256 bits, so there is no
dictionary to attack and a plain digest is enough. What it may do is fixed
when it is made; it is revoked, never edited, so a key's history is its
creation and its revocation, both in the audit trail.

Whoever hands out a key cannot give more than they hold: no permission they
lack, and a caller held to row rules hands out nothing, since a new key
without them would be a way out. A key carries exactly its grantor's roles,
in order: a role can lift masks and row rules, and since a role's own masking
rules replace the default ones, leaving a role off (or reordering them) can
loosen masks as surely as adding one.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import Any

#: What a key may be granted. ``admin`` is handing out and revoking keys.
PERMISSIONS = ("read", "create", "update", "delete", "query", "audit", "admin", "all")

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}$")
#: Marks a secret as one of these keys, so a leaked one is easy to search for.
SECRET_PREFIX = "trio_"


class AccessKeyError(ValueError):
    """A key that cannot be made or revoked, with the reason why."""


def new_secret() -> str:
    """A fresh key: the prefix and 256 random bits."""
    return SECRET_PREFIX + secrets.token_urlsafe(32)


def digest(secret: str) -> str:
    """What is stored and compared instead of the key."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ManagedKey:
    """One handed-out key, as stored: everything but the secret."""

    id: str
    name: str
    digest: str
    #: The secret's last four characters, so a holder can tell which key they have.
    hint: str
    permissions: tuple[str, ...]
    created_at: datetime
    created_by: str | None
    tenant: str | None = None
    roles: tuple[str, ...] = ()
    #: table -> [{column, value}], as a configured key's ``row_filters``.
    row_filters: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    expires_at: datetime | None = None
    revoked_at: datetime | None = None
    revoked_by: str | None = None

    def active(self, now: datetime) -> bool:
        """Whether the key still lets its holder in."""
        return self.revoked_at is None and (self.expires_at is None or now < self.expires_at)

    def revoke(self, by: str | None, now: datetime) -> ManagedKey:
        """The same key, revoked."""
        return replace(self, revoked_at=now, revoked_by=by)

    def as_record(self) -> dict[str, Any]:
        """The stored form (JSON-safe); carries the digest, never the secret."""
        return {**self.as_public(), "digest": self.digest}

    def as_public(self, now: datetime | None = None) -> dict[str, Any]:
        """What an administrator is shown: no digest."""
        out: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "hint": self.hint,
            "permissions": list(self.permissions),
            "tenant": self.tenant,
            "roles": list(self.roles),
            "row_filters": self.row_filters,
            "created_at": self.created_at.isoformat(),
            "created_by": self.created_by,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "revoked_at": self.revoked_at.isoformat() if self.revoked_at else None,
            "revoked_by": self.revoked_by,
        }
        if now is not None:
            out["active"] = self.active(now)
        return out

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> ManagedKey:
        """A stored key read back."""

        def when(value: Any) -> datetime | None:
            return datetime.fromisoformat(value) if value else None

        created = when(record["created_at"])
        assert created is not None
        return cls(
            id=str(record["id"]),
            name=str(record["name"]),
            digest=str(record["digest"]),
            hint=str(record.get("hint", "")),
            permissions=tuple(record.get("permissions", ())),
            created_at=created,
            created_by=record.get("created_by"),
            tenant=record.get("tenant"),
            roles=tuple(record.get("roles", ())),
            row_filters=dict(record.get("row_filters") or {}),
            expires_at=when(record.get("expires_at")),
            revoked_at=when(record.get("revoked_at")),
            revoked_by=record.get("revoked_by"),
        )


@dataclass(frozen=True, slots=True)
class Grantor:
    """Who is handing out a key, as far as what they may give goes."""

    name: str | None
    permissions: tuple[str, ...]
    roles: tuple[str, ...]
    has_row_rules: bool


def check_grant(grantor: Grantor, permissions: list[str], roles: list[str]) -> None:
    """Refuse a key that would give more than its grantor holds, or other roles.

    Raises:
        AccessKeyError: Naming what cannot be given.
    """
    unknown = sorted(set(permissions) - set(PERMISSIONS))
    if unknown:
        raise AccessKeyError(f"Unknown permission(s): {', '.join(unknown)}")
    if not permissions:
        raise AccessKeyError("A key needs at least one permission")
    if grantor.has_row_rules:
        raise AccessKeyError(
            "You are held to row rules, so you cannot hand out keys: one without them "
            "would see what you cannot"
        )
    if "all" not in grantor.permissions:
        beyond = sorted(set(permissions) - set(grantor.permissions))
        if beyond:
            raise AccessKeyError(f"You cannot give what you do not hold: {', '.join(beyond)}")
    # Even "all" gives no other roles: roles decide masks, which permissions do not cover.
    if tuple(roles) != tuple(grantor.roles):
        yours = ", ".join(grantor.roles) or "none"
        raise AccessKeyError(
            f"A key carries the roles of whoever hands it out (yours: {yours}): a role can "
            "lift masks and row rules, and leaving one off can loosen them"
        )


def now() -> datetime:
    """The current time, in UTC."""
    return datetime.now(UTC)
