"""Signing in with a directory's username and password (lldap, OpenLDAP, Active Directory).

The steps, and what each one guards against:

1. The username is escaped into the search filter, so ``*`` or ``)(`` in it
   cannot widen the search or change what it asks.
2. A service account finds the user. Exactly one entry must match: none and
   several are both a refusal.
3. The password is checked by binding as the user. An empty password is
   refused *before* it gets there: many servers treat a bind with a name and no
   password as an anonymous one, and answer success.
4. Wrong username and wrong password give the same answer, so the response
   does not say which usernames exist.
5. Repeated failures pause sign-in for that username and that address
   (:class:`LoginThrottle`), so the directory is not a password-guessing oracle.
"""

from __future__ import annotations

import logging
import math
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ldap3 import NONE, Connection, Server, Tls
from ldap3.core.exceptions import LDAPException, LDAPSocketOpenError
from ldap3.utils.conv import escape_filter_chars
from ldap3.utils.dn import parse_dn

from truejoin.application.settings import ApiKeyConfig, LdapConfig
from truejoin.domain.governance.signin import (
    REFUSED,
    LoginThrottle,
    SignInRefused,
    TooManyFailures,
)

__all__ = [
    "REFUSED",
    "DirectoryUnreachable",
    "DirectoryUser",
    "LdapAuthenticator",
    "LoginThrottle",
    "SignInRefused",
    "TooManyFailures",
]

logger = logging.getLogger(__name__)

#: A username longer than this is not one.
MAX_USERNAME = 256
MAX_PASSWORD = 1024


class DirectoryUnreachable(Exception):
    """The directory did not answer: worth trying again, unlike a refusal."""


@dataclass(frozen=True, slots=True)
class DirectoryUser:
    """Who the directory says signed in."""

    username: str
    dn: str
    display: str | None = None
    email: str | None = None
    tenant: str | None = None
    groups: tuple[str, ...] = ()
    extra: dict[str, str] = field(default_factory=dict)


def _first(value: object) -> str | None:
    """One text value from an attribute that may be a list, or empty."""
    if isinstance(value, list | tuple):
        value = value[0] if value else None
    if value is None or value == "":
        return None
    return str(value)


def _group_name(dn: str, attribute: str = "cn") -> str:
    """The group's name from its DN: the value of the first ``cn=`` (or ``attribute``) part."""
    try:
        for part, value, _ in parse_dn(dn):
            if part.lower() == attribute.lower():
                return value
        return parse_dn(dn)[0][1]
    except LDAPException:
        return dn


class LdapAuthenticator:
    """Checks a username and password against one directory and says who they are."""

    def __init__(
        self,
        config: LdapConfig,
        clock: Callable[[], float] = time.monotonic,
        connection_factory: Callable[..., Connection] = Connection,
    ) -> None:
        """Keep the configuration; nothing is contacted until the first sign-in.

        ``connection_factory`` builds each connection (a test hands in one over
        ldap3's in-memory directory).
        """
        self.config = config
        self._connection = connection_factory
        self.throttle = LoginThrottle(config.max_failures, config.lockout_seconds, clock)
        # Behind a proxy every person shares an address, so one address may fail ten
        # times as often as one username before it is paused: a limit on guessing
        # across many usernames, not a way to lock a whole office out.
        self.address_throttle = LoginThrottle(
            config.max_failures * 10, config.lockout_seconds, clock
        )
        self._server: Server | None = None

    # -- connecting ---------------------------------------------------------

    def _tls(self) -> Tls:
        return Tls(
            validate=ssl.CERT_REQUIRED,
            ca_certs_file=self.config.tls_ca_file,
            version=ssl.PROTOCOL_TLS_CLIENT,
        )

    def _directory(self) -> Server:
        if self._server is None:
            ldaps = self.config.url.lower().startswith("ldaps://")
            self._server = Server(
                self.config.url,
                get_info=NONE,
                connect_timeout=self.config.timeout_seconds,
                tls=self._tls() if (ldaps or self.config.start_tls) else None,
            )
        return self._server

    def _connect(self, dn: str, password: str) -> Connection:
        """A bound connection; a refused bind raises :class:`SignInRefused`."""
        connection: Connection | None = None
        try:
            connection = self._connection(
                self._directory(),
                user=dn,
                password=password,
                # ldap3 packs this into a socket option as a whole number of seconds.
                receive_timeout=max(1, math.ceil(self.config.timeout_seconds)),
                raise_exceptions=False,
                auto_bind=False,
            )
            if self.config.start_tls and not connection.start_tls():
                raise DirectoryUnreachable("The directory refused to start an encrypted session")
            if not connection.bind():
                result = connection.result or {}
                # 49 = invalid credentials. Anything else is the directory's trouble, not theirs.
                if result.get("result") == 49:
                    raise SignInRefused(REFUSED)
                logger.warning("LDAP bind as %s failed: %s", dn, result.get("description"))
                raise DirectoryUnreachable("The directory could not check the sign-in")
        except (SignInRefused, DirectoryUnreachable):
            self._release(connection)
            raise
        except (LDAPSocketOpenError, OSError) as e:
            self._release(connection)
            logger.warning("Could not reach the directory at %s: %s", self.config.url, e)
            raise DirectoryUnreachable("The directory cannot be reached") from e
        except LDAPException as e:
            self._release(connection)
            logger.warning("LDAP error talking to %s: %s", self.config.url, e)
            raise DirectoryUnreachable("The directory cannot be reached") from e
        return connection

    @staticmethod
    def _release(connection: Connection | None) -> None:
        if connection is not None:
            try:
                connection.unbind()
            except (LDAPException, OSError):  # already gone: nothing left to close
                pass

    # -- signing in ---------------------------------------------------------

    def authenticate(self, username: str, password: str, client: str = "") -> DirectoryUser:
        """The directory user who owns ``password``.

        Raises:
            TooManyFailures: This user or address failed too often recently.
            SignInRefused: The username or password is wrong, or the person may not sign in.
            DirectoryUnreachable: The directory did not answer.
        """
        username = (username or "").strip()
        if (
            not username
            or not password
            or len(username) > MAX_USERNAME
            or len(password) > MAX_PASSWORD
            or any(ord(c) < 32 for c in username)
        ):
            # Before any bind: an empty password is an anonymous bind to many servers.
            raise SignInRefused(REFUSED)
        who = username.lower()
        self.throttle.check(who)
        if client:
            self.address_throttle.check(client)
        try:
            user = self._authenticate(username, password)
        except SignInRefused:
            self.throttle.failed(who)
            if client:
                self.address_throttle.failed(client)
            raise
        self.throttle.succeeded(who)  # the address keeps its count: one success proves nothing
        return user

    def _authenticate(self, username: str, password: str) -> DirectoryUser:
        config = self.config
        service = self._connect(config.bind_dn, config.bind_password)
        try:
            entry = self._find_user(service, username)
            groups = self._groups(service, entry)
        finally:
            service.unbind()
        dn = entry["dn"]
        self._connect(dn, password).unbind()  # a wrong password raises SignInRefused
        if config.allowed_groups and not {g.lower() for g in groups} & {
            g.lower() for g in config.allowed_groups
        }:
            logger.info("Sign-in refused for %s: not in an allowed group", username)
            raise SignInRefused("This account may not sign in here")
        attributes = entry["attributes"]
        return DirectoryUser(
            username=_first(attributes.get(config.username_attr)) or username,
            dn=dn,
            display=_first(attributes.get(config.display_attr)) if config.display_attr else None,
            email=_first(attributes.get(config.email_attr)) if config.email_attr else None,
            tenant=_first(attributes.get(config.tenant_attr)) if config.tenant_attr else None,
            groups=tuple(groups),
        )

    def _find_user(self, service: Connection, username: str) -> dict[str, Any]:
        config = self.config
        wanted = [
            config.username_attr,
            config.display_attr,
            config.email_attr,
            config.tenant_attr,
            config.member_of_attr,
        ]
        found = service.search(
            config.user_base,
            config.user_filter.format(username=escape_filter_chars(username)),
            attributes=[a for a in wanted if a],
            size_limit=2,
        )
        entries = [e for e in service.response or [] if e.get("type") == "searchResEntry"]
        if not found or len(entries) != 1:
            if len(entries) > 1:
                logger.warning("auth.ldap.user_filter matched several entries for one username")
            raise SignInRefused(REFUSED)
        return entries[0]

    def _groups(self, service: Connection, entry: dict[str, Any]) -> list[str]:
        config = self.config
        if config.member_of_attr:
            raw = entry["attributes"].get(config.member_of_attr) or []
            raw = [raw] if isinstance(raw, str) else raw
            return sorted({_group_name(dn, config.group_name_attr) for dn in raw})
        assert config.group_base is not None
        service.search(
            config.group_base,
            config.group_filter.format(user_dn=escape_filter_chars(entry["dn"])),
            attributes=[config.group_name_attr],
        )
        names = {
            _first(e["attributes"].get(config.group_name_attr))
            for e in service.response or []
            if e.get("type") == "searchResEntry"
        }
        return sorted(n for n in names if n)

    # -- who they are, as the rest of the server sees a caller -----------------

    def roles(self, groups: list[str] | tuple[str, ...]) -> list[str]:
        """The roles a person's groups give: mapped ones, and any group's own name."""
        mapped = {g.lower(): roles for g, roles in self.config.group_roles.items()}
        out: list[str] = []
        for group in groups:
            for role in mapped.get(group.lower(), [group]):
                if role not in out:
                    out.append(role)
        return out

    def caller(self, name: str, roles: list[str], tenant: str | None) -> ApiKeyConfig:
        """The caller a sign-in names, in the shape an API key has."""
        config = self.config
        permissions = list(
            dict.fromkeys(
                [
                    *config.default_permissions,
                    *(p for role in roles for p in config.role_permissions.get(role, [])),
                ]
            )
        )
        exempt = any(role in config.row_filter_exempt_roles for role in roles)
        return ApiKeyConfig(
            key="",  # nothing to compare: the directory was the proof
            name=name,
            permissions=permissions,
            tenant=tenant,
            roles=roles,
            row_filters={} if exempt else config.row_filters,
        )
