"""Making, changing and signing in the local users an administrator manages.

Like the handed-out keys, the users are read again whenever the store says they
changed, and every change is a read, an edit and a write under the store's lock,
so two administrators (or two workers) cannot undo each other's work.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from collections.abc import Callable, Hashable, Iterable
from datetime import datetime

from truejoin.application.settings import ApiKeyConfig, LocalUsersConfig
from truejoin.domain.governance.keys import Grantor, now
from truejoin.domain.governance.signin import REFUSED, LoginThrottle, SignInRefused
from truejoin.domain.governance.users import (
    DUMMY_HASH,
    MAX_PASSWORD,
    ManagedUser,
    UserError,
    check_may_manage,
    check_name,
    check_password,
    check_user_grant,
    hash_password,
    verify_password,
)
from truejoin.ports.user_store import UserStore

logger = logging.getLogger(__name__)

#: A username longer than this is not one.
MAX_USERNAME = 64


class LocalUsers:
    """The local users, and who among them may sign in."""

    def __init__(
        self,
        store: UserStore,
        config: LocalUsersConfig,
        reserved: Iterable[str] = (),
        clock: Callable[[], datetime] = now,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        """``reserved`` are names a user may not take (the configured keys')."""
        self._store = store
        self.config = config
        self._reserved = {name.lower() for name in reserved}
        self._clock = clock
        self._lock = threading.Lock()
        self._users: list[ManagedUser] = []
        self._version: Hashable = object()  # matches nothing: the first read loads
        self.throttle = LoginThrottle(config.max_failures, config.lockout_seconds, monotonic)
        # Behind a proxy everyone shares an address: ten times the failures of one username.
        self.address_throttle = LoginThrottle(
            config.max_failures * 10, config.lockout_seconds, monotonic
        )

    # -- reading ------------------------------------------------------------

    def _fresh(self) -> list[ManagedUser]:
        version = self._store.version()
        if version != self._version:
            try:
                self._users = self._store.load()
                self._version = version
            except Exception as e:  # a damaged file must not lock everyone out
                logger.error("Could not read the local users; keeping the last read: %s", e)
        return self._users

    def users(self) -> list[ManagedUser]:
        """Every local user, disabled ones included, by name."""
        with self._lock:
            return sorted(self._fresh(), key=lambda u: u.name.lower())

    def get(self, name: str) -> ManagedUser | None:
        """The user called ``name`` (any case), or None."""
        wanted = name.lower()
        with self._lock:
            return next((u for u in self._fresh() if u.name.lower() == wanted), None)

    def caller_for(self, name: str) -> ApiKeyConfig | None:
        """The caller a signed-in user is *now*: their stored permissions, if they may still sign in."""
        user = self.get(name)
        if user is None or user.disabled:
            return None
        return ApiKeyConfig(
            key="",  # nothing to compare: the password was the proof
            name=user.name,
            permissions=list(user.permissions),
            tenant=user.tenant,
            roles=list(user.roles),
        )

    # -- signing in ---------------------------------------------------------

    def authenticate(self, name: str, password: str, client: str = "") -> ManagedUser:
        """The user who owns ``password``.

        Raises:
            SignInRefused: Wrong name or password, or the account is disabled.
            TooManyFailures: This name or address failed too often recently.
        """
        name = (name or "").strip()
        if (
            not name
            or not password
            or len(name) > MAX_USERNAME
            or len(password) > MAX_PASSWORD
            or any(ord(c) < 32 for c in name)
        ):
            raise SignInRefused(REFUSED)
        who = name.lower()
        self.throttle.check(who)
        if client:
            self.address_throttle.check(client)
        user = self.get(name)
        # A name that does not exist costs as much as a wrong password: the time says nothing.
        right = verify_password(password, user.password_hash if user else DUMMY_HASH)
        if user is None or user.disabled or not right:
            self.throttle.failed(who)
            if client:
                self.address_throttle.failed(client)
            raise SignInRefused(REFUSED)
        self.throttle.succeeded(who)
        self._touch(user.name)
        return user

    def _touch(self, name: str) -> None:
        """Record the sign-in; a failure to write is not a failure to sign in."""
        try:
            with self._lock, self._store.locked():
                users = self._reload()
                when = self._clock()
                self._save([u.changed(last_sign_in=when) if u.name == name else u for u in users])
        except Exception as e:
            logger.warning("Could not record the sign-in of %s: %s", name, e)

    # -- changing -----------------------------------------------------------

    def create(
        self,
        grantor: Grantor,
        *,
        name: str,
        password: str,
        permissions: list[str],
        display: str | None = None,
        tenant: str | None = None,
        roles: list[str] | None = None,
    ) -> ManagedUser:
        """A new user. ``roles`` left out means the grantor's own, the only ones allowed.

        Raises:
            UserError: Why it cannot be made.
        """
        roles = list(grantor.roles) if roles is None else list(roles)
        permissions = list(dict.fromkeys(permissions))
        check_name(name)
        check_password(password, name, self.config.min_password_length)
        check_user_grant(grantor, permissions, roles)
        hashed = hash_password(password)
        when = self._clock()
        with self._lock, self._store.locked():
            users = self._reload()
            taken = self._reserved | {u.name.lower() for u in users}
            if name.lower() in taken:
                raise UserError(f"A user or key called {name} already exists")
            user = ManagedUser(
                name=name,
                password_hash=hashed,
                permissions=tuple(permissions),
                created_at=when,
                created_by=grantor.name,
                display=(display or "").strip() or None,
                tenant=tenant or None,
                roles=tuple(roles),
                password_changed_at=when,
            )
            self._save([*users, user])
        return user

    def update(
        self,
        grantor: Grantor,
        name: str,
        *,
        permissions: list[str] | None = None,
        display: str | None = None,
        tenant: str | None = None,
        disabled: bool | None = None,
        clear_tenant: bool = False,
    ) -> ManagedUser:
        """Change what a user may do, who they are shown as, or whether they may sign in.

        Raises:
            LookupError: No such user.
            UserError: Why it cannot be done.
        """
        with self._lock, self._store.locked():
            users = self._reload()
            target = self._find(users, name)
            check_may_manage(grantor, target)
            myself = bool(grantor.name) and target.name.lower() == (grantor.name or "").lower()
            if myself and (permissions is not None or disabled or tenant or clear_tenant):
                raise UserError("You cannot change your own permissions or sign yourself out")
            fields: dict[str, object] = {"updated_at": self._clock()}
            if permissions is not None:
                wanted = list(dict.fromkeys(permissions))
                check_user_grant(grantor, wanted, list(target.roles))
                fields["permissions"] = tuple(wanted)
            if display is not None:
                fields["display"] = display.strip() or None
            if tenant is not None:
                fields["tenant"] = tenant or None
            if clear_tenant:
                fields["tenant"] = None
            if disabled is not None:
                fields["disabled"] = disabled
            changed = target.changed(**fields)
            self._save([changed if u.name == target.name else u for u in users])
        return changed

    def set_password(self, grantor: Grantor, name: str, password: str) -> ManagedUser:
        """An administrator sets another user's password; it ends that user's sign-ins.

        Raises:
            LookupError: No such user.
            UserError: Why it cannot be done (including: it is your own; use *change*).
        """
        with self._lock, self._store.locked():
            users = self._reload()
            target = self._find(users, name)
            check_may_manage(grantor, target)
            if bool(grantor.name) and target.name.lower() == (grantor.name or "").lower():
                raise UserError("Change your own password with your current one")
            check_password(password, target.name, self.config.min_password_length)
            changed = self._with_password(target, password)
            self._save([changed if u.name == target.name else u for u in users])
        self.throttle.succeeded(target.name.lower())  # a reset also lifts a pause
        return changed

    def change_password(self, name: str, current: str, new: str) -> ManagedUser:
        """A user changes their own password, proving they know the current one.

        Raises:
            LookupError: No such user.
            SignInRefused: The current password is wrong.
            UserError: The new one will not do.
        """
        who = name.lower()
        self.throttle.check(who)
        with self._lock, self._store.locked():
            users = self._reload()
            target = self._find(users, name)
            if target.disabled or not verify_password(current or "", target.password_hash):
                self.throttle.failed(who)
                raise SignInRefused("The current password is wrong")
            check_password(new, target.name, self.config.min_password_length)
            if new == current:
                raise UserError("The new password must differ from the current one")
            changed = self._with_password(target, new)
            self._save([changed if u.name == target.name else u for u in users])
        self.throttle.succeeded(who)
        return changed

    def delete(self, grantor: Grantor, name: str) -> ManagedUser:
        """Remove a user; whatever they were signed in with stops working at once.

        Raises:
            LookupError: No such user.
            UserError: Why it cannot be done.
        """
        with self._lock, self._store.locked():
            users = self._reload()
            target = self._find(users, name)
            check_may_manage(grantor, target)
            if bool(grantor.name) and target.name.lower() == (grantor.name or "").lower():
                raise UserError("You cannot remove the account you are using")
            self._save([u for u in users if u.name != target.name])
        return target

    # -- helpers ------------------------------------------------------------

    def _with_password(self, user: ManagedUser, password: str) -> ManagedUser:
        when = self._clock()
        return user.changed(
            password_hash=hash_password(password), password_changed_at=when, updated_at=when
        )

    @staticmethod
    def _find(users: list[ManagedUser], name: str) -> ManagedUser:
        wanted = name.lower()
        found = next((u for u in users if u.name.lower() == wanted), None)
        if found is None:
            raise LookupError(f"No user {name}")
        return found

    def _reload(self) -> list[ManagedUser]:
        """The stored users as they are now; called under the store's lock."""
        self._version = self._store.version()
        self._users = self._store.load()
        return self._users

    def _save(self, users: list[ManagedUser]) -> None:
        """Write, under the store's lock, so the version read back is this write's."""
        self._store.save(users)
        self._users = users
        self._version = self._store.version()


def password_version(user: ManagedUser) -> str:
    """What a session remembers of the password it was made with: it ends when this changes.

    A fingerprint of the stored hash, which is salted, so any new password (even the same
    words again) gives a different one; a timestamp could repeat within its resolution.
    """
    return hashlib.sha256(user.password_hash.encode("utf-8")).hexdigest()[:16]
