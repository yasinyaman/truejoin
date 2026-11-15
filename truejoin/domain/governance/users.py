"""Local users an administrator makes from the console, beside the directory and the keys.

A user has a name and a password; what they may do is fixed by permissions,
as for an API key. The password is kept only as a scrypt hash (salted, slow on
purpose): unlike a key, which is 256 random bits, a password is something a
person chose, so a plain digest would not be enough.

Who may make and change users follows the rule for keys. Nobody gives more than
they hold, a caller held to row rules makes nobody (a user without them would
see what the maker cannot), and a user carries exactly its maker's roles
(a role can lift masks and row rules, and leaving one off can loosen them).
Nobody manages an account stronger than their own: resetting a password
would be taking the account over.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

from truejoin.domain.governance.keys import NAME_RE, PERMISSIONS, Grantor

#: Names that mean something to the routes or the console, so no person is called one.
RESERVED_NAMES = frozenset({"me", "admin", "root", "system"})

MAX_PASSWORD = 1024
#: scrypt cost: N * r * 128 bytes of memory (32 MiB), tens of milliseconds a check.
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**15, 8, 1
#: The most a stored hash may ask of the server; a damaged or planted record cannot ask for more.
_MAX_N, _MAX_R, _MAX_P = 2**17, 16, 4
_SCRYPT_MEMORY = 256 * 1024 * 1024
_HASH_RE = re.compile(r"^scrypt\$(\d+)\$(\d+)\$(\d+)\$([A-Za-z0-9_-]+)\$([A-Za-z0-9_-]+)$")


class UserError(ValueError):
    """A user that cannot be made or changed, with the reason why."""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=_SCRYPT_MEMORY, dklen=32
    )


def hash_password(password: str) -> str:
    """``scrypt$N$r$p$salt$hash``: everything needed to check it later, and nothing to reverse."""
    salt = secrets.token_bytes(16)
    key = _derive(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P)
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${_b64(salt)}${_b64(key)}"


def verify_password(password: str, stored: str) -> bool:
    """Whether ``password`` is the one ``stored`` was made from (never raises)."""
    match = _HASH_RE.match(stored or "")
    if not match or len(password) > MAX_PASSWORD:
        return False
    n, r, p = (int(match.group(i)) for i in (1, 2, 3))
    if not (2 <= n <= _MAX_N and n & (n - 1) == 0 and 1 <= r <= _MAX_R and 1 <= p <= _MAX_P):
        return False
    try:
        expected = _unb64(match.group(5))
        actual = _derive(password, _unb64(match.group(4)), n, r, p)
    except (ValueError, OverflowError):
        return False
    return hmac.compare_digest(actual, expected)


#: Checked when the user does not exist, so a missing name takes as long as a wrong password.
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def check_password(password: str, username: str, minimum: int) -> None:
    """Refuse a password that is too short, too long, or says who it belongs to.

    Length is what matters (NIST SP 800-63B): no rules about which characters
    it must contain, which only teach people to write ``Password1!``.

    Raises:
        UserError: Why it will not do.
    """
    if len(password) < minimum:
        raise UserError(f"A password is at least {minimum} characters")
    if len(password) > MAX_PASSWORD:
        raise UserError(f"A password is at most {MAX_PASSWORD} characters")
    if password.strip() != password:
        raise UserError("A password cannot start or end with a space")
    if password.lower() in {username.lower(), username.lower()[::-1]}:
        raise UserError("A password cannot be the username")
    if len(set(password)) < 4:
        raise UserError("A password cannot be made of a few repeated characters")


def check_name(name: str) -> None:
    """Refuse a name that is not a sign-in name, or means something else here."""
    if not NAME_RE.match(name):
        raise UserError(
            "A name is 1 to 64 letters, digits and . _ @ -, starting with a letter or digit"
        )
    if name.lower() in RESERVED_NAMES:
        raise UserError(f"{name} is reserved: choose another name")


@dataclass(frozen=True, slots=True)
class ManagedUser:
    """One local user, as stored: the password hash and never the password."""

    name: str
    password_hash: str
    permissions: tuple[str, ...]
    created_at: datetime
    created_by: str | None
    display: str | None = None
    tenant: str | None = None
    roles: tuple[str, ...] = ()
    disabled: bool = False
    updated_at: datetime | None = None
    password_changed_at: datetime | None = None
    last_sign_in: datetime | None = None

    def changed(self, **fields: Any) -> ManagedUser:
        """The same user with some fields replaced."""
        return replace(self, **fields)

    def as_record(self) -> dict[str, Any]:
        """The stored form (JSON-safe); carries the hash."""
        return {**self.as_public(), "password_hash": self.password_hash}

    def as_public(self) -> dict[str, Any]:
        """What an administrator is shown: no hash."""

        def iso(value: datetime | None) -> str | None:
            return value.isoformat() if value else None

        return {
            "name": self.name,
            "display": self.display,
            "permissions": list(self.permissions),
            "tenant": self.tenant,
            "roles": list(self.roles),
            "disabled": self.disabled,
            "created_at": iso(self.created_at),
            "created_by": self.created_by,
            "updated_at": iso(self.updated_at),
            "password_changed_at": iso(self.password_changed_at),
            "last_sign_in": iso(self.last_sign_in),
        }

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> ManagedUser:
        """A stored user read back."""

        def when(value: Any) -> datetime | None:
            return datetime.fromisoformat(value) if value else None

        created = when(record["created_at"])
        assert created is not None
        return cls(
            name=str(record["name"]),
            password_hash=str(record["password_hash"]),
            permissions=tuple(record.get("permissions", ())),
            created_at=created,
            created_by=record.get("created_by"),
            display=record.get("display"),
            tenant=record.get("tenant"),
            roles=tuple(record.get("roles", ())),
            disabled=bool(record.get("disabled", False)),
            updated_at=when(record.get("updated_at")),
            password_changed_at=when(record.get("password_changed_at")),
            last_sign_in=when(record.get("last_sign_in")),
        )


def check_user_grant(grantor: Grantor, permissions: list[str], roles: list[str]) -> None:
    """Refuse a user that would hold more than whoever makes it, or other roles.

    Raises:
        UserError: Naming what cannot be given.
    """
    unknown = sorted(set(permissions) - set(PERMISSIONS))
    if unknown:
        raise UserError(f"Unknown permission(s): {', '.join(unknown)}")
    if not permissions:
        raise UserError("A user needs at least one permission")
    if grantor.has_row_rules:
        raise UserError(
            "You are held to row rules, so you cannot make users: one without them "
            "would see what you cannot"
        )
    if "all" not in grantor.permissions:
        beyond = sorted(set(permissions) - set(grantor.permissions))
        if beyond:
            raise UserError(f"You cannot give what you do not hold: {', '.join(beyond)}")
    if tuple(roles) != tuple(grantor.roles):
        yours = ", ".join(grantor.roles) or "none"
        raise UserError(
            f"A user carries the roles of whoever makes it (yours: {yours}): a role can "
            "lift masks and row rules, and leaving one off can loosen them"
        )


def check_may_manage(grantor: Grantor, target: ManagedUser) -> None:
    """Refuse changing an account that is stronger than the one changing it.

    Raises:
        UserError: Why this account is out of reach.
    """
    if grantor.has_row_rules:
        raise UserError("You are held to row rules, so you cannot manage users")
    if "all" not in grantor.permissions and "all" in target.permissions:
        raise UserError(f"{target.name} holds more than you do: it is not yours to change")
    if "all" not in grantor.permissions:
        beyond = sorted(set(target.permissions) - set(grantor.permissions))
        if beyond:
            raise UserError(f"{target.name} holds more than you do ({', '.join(beyond)})")
    if tuple(target.roles) != tuple(grantor.roles):
        raise UserError(f"{target.name} carries other roles than yours: it is not yours to change")
