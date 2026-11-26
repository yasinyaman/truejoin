"""Authentication and authorization module for API endpoints."""

import asyncio
import hashlib
import logging
import secrets
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Any, NoReturn

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import APIKeyHeader

from truejoin.adapters.inbound.http.ldap import LdapAuthenticator
from truejoin.adapters.inbound.http.oidc import OidcVerifier, ProviderUnreachable, TokenError
from truejoin.adapters.inbound.http.sessions import SessionError, SessionTokens, is_session_token
from truejoin.application.access_keys import AccessKeyService
from truejoin.application.local_users import LocalUsers, password_version
from truejoin.application.settings import ApiKeyConfig, AuthConfig
from truejoin.domain.governance.row_policy import EMPTY_POLICY, PolicyError, RowPolicy, build_policy
from truejoin.domain.governance.session import CURRENT_IDENTITY, SessionIdentity
from truejoin.domain.governance.signin import REFUSED, SignInRefused, TooManyFailures

logger = logging.getLogger(__name__)


class Permission(StrEnum):
    """Available permissions for API operations."""

    READ = "read"
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"
    QUERY = "query"  # For raw SQL queries
    AUDIT = "audit"  # Read the audit trail
    ADMIN = "admin"  # Hand out and revoke API keys
    ALL = "all"


class AuthenticatedUser:
    """Represents an authenticated API user."""

    def __init__(self, api_key_config: ApiKeyConfig):
        """Store the identity, permissions and row policy from the API-key config."""
        self.name = api_key_config.name
        # Deliberately do NOT retain the plaintext API key on the user object.
        self.permissions = api_key_config.permissions
        self.tenant = api_key_config.tenant
        self.roles = list(api_key_config.roles)
        self.row_policy = build_policy(
            {
                table: [rule.model_dump() for rule in rules]
                for table, rules in api_key_config.row_filters.items()
            },
            caller={"tenant": api_key_config.tenant, "username": api_key_config.name},
        )

    @property
    def has_row_rules(self) -> bool:
        """Whether any row rule restricts what this caller may read or write."""
        return not self.row_policy.is_empty

    def has_permission(self, permission: Permission) -> bool:
        """Check if user has the required permission.

        ``query`` is the exception: raw SQL cannot have row conditions pushed
        into it, so a caller with row rules is never allowed to run it —
        otherwise the rules would be one ``SELECT`` away from irrelevant.
        """
        if permission is Permission.QUERY and self.has_row_rules:
            return False
        return self.grants(permission)

    def grants(self, permission: Permission) -> bool:
        """Whether the key lists ``permission``, whatever its row rules say.

        The analytical endpoints ask this instead of ``has_permission``: they
        rewrite the caller's row rules into the SQL, so the reason a key with
        row rules is kept away from raw SQL does not apply there.
        """
        if Permission.ALL.value in self.permissions:
            return True
        return permission.value in self.permissions


class AuthManager:
    """Manages API authentication and authorization.

    Usage:
        auth_manager = AuthManager(auth_config)

        # In router:
        @router.get("/items", dependencies=[Depends(auth_manager.require(Permission.READ))])
        async def list_items():
            ...
    """

    def __init__(
        self,
        auth_config: AuthConfig,
        oidc: OidcVerifier | None = None,
        keys: AccessKeyService | None = None,
        ldap: LdapAuthenticator | None = None,
        sessions: SessionTokens | None = None,
        users: LocalUsers | None = None,
    ):
        """Index API keys by hash and set up the header scheme.

        ``oidc`` checks bearer tokens; by default it is built from
        ``auth_config.oidc`` (a test hands in one with its own key source).
        ``keys`` holds the keys handed out from the console, looked up when a
        presented key is not one of the configured ones. ``ldap`` signs people
        in with a directory password (built from ``auth_config.ldap``) and
        ``sessions`` checks the tokens it hands them. ``users`` are the local
        users made from the console, who sign in the same way.
        """
        self.keys = keys
        self.config = auth_config
        self.enabled = auth_config.enabled
        self.header_name = auth_config.header_name
        # Store sha256(key) -> config instead of plaintext keys. Empty/unset
        # keys are dropped so they can never authenticate.
        self._key_hashes: list[tuple[str, ApiKeyConfig]] = [
            (self._hash_key(key.key), key) for key in auth_config.api_keys if key.key
        ]
        self.api_key_count = len(self._key_hashes)
        self.public_paths = auth_config.public_paths

        self.oidc = oidc or (OidcVerifier(auth_config.oidc) if auth_config.oidc else None)
        self.ldap = ldap or (LdapAuthenticator(auth_config.ldap) if auth_config.ldap else None)
        self.users = users
        signs_in = auth_config.ldap or (users.config if users else None)
        self.sessions = sessions or (
            SessionTokens(signs_in.session_ttl_seconds) if signs_in else None
        )

        # Create API key header scheme
        self.api_key_header = APIKeyHeader(
            name=self.header_name, auto_error=False, description="API Key for authentication"
        )

    @staticmethod
    def _hash_key(api_key: str) -> str:
        """Hash an API key for constant-length, timing-safe comparison.

        Unkeyed, unlike the masking module's `hash` strategy, and deliberately
        so: the input here is a generated token with ~256 bits of entropy, so
        there is no dictionary to attack. The masking case hashes emails and
        national ids, which are enumerable — that is the difference, not the
        algorithm.
        """
        return hashlib.sha256(api_key.encode("utf-8")).hexdigest()

    def _get_api_key_config(self, api_key: str | None) -> ApiKeyConfig | None:
        """Look up the config for a presented key using a timing-safe comparison.

        Uses ``secrets.compare_digest`` against the stored hashes and scans all
        configured keys without short-circuiting, so neither the comparison nor
        the number of keys leaks timing information about the secret.
        """
        if not api_key:
            return None
        candidate = self._hash_key(api_key)
        match: ApiKeyConfig | None = None
        for stored_hash, config in self._key_hashes:
            if secrets.compare_digest(stored_hash, candidate):
                match = config
        if match is None and self.keys is not None:
            return self.keys.caller_for(api_key)
        return match

    def _is_public_path(self, path: str) -> bool:
        """Check if the path is public (no auth required).

        A public path matches itself and its sub-paths on a segment boundary
        (``/docs`` covers ``/docs/oauth2-redirect`` but not ``/docsx``).
        """
        for public_path in self.public_paths:
            base = public_path.rstrip("/")
            if not base:  # "/" makes everything public
                return True
            if path == base or path.startswith(base + "/"):
                return True
        return False

    async def get_current_user(
        self, request: Request, api_key: str | None = None
    ) -> AuthenticatedUser | None:
        """Get the current authenticated user from API key.

        Returns None if auth is disabled or path is public.
        Raises HTTPException if auth is required but invalid.
        """
        # If auth is disabled, allow all
        if not self.enabled:
            return None

        # Check if path is public
        if self._is_public_path(request.url.path):
            return None

        # Get API key from header
        if api_key is None:
            api_key = request.headers.get(self.header_name)

        if not api_key and self.accepts_bearer:
            scheme, _, token = request.headers.get("authorization", "").partition(" ")
            if scheme.lower() == "bearer" and token.strip():
                return await self._bearer_user(request, token.strip())

        if not api_key:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="API key required"
                if not self.accepts_bearer
                else f"An API key ({self.header_name}) or a bearer token is required",
                headers=self._challenge(request),
            )

        # Validate API key
        key_config = self._get_api_key_config(api_key)
        if key_config is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid API key",
                headers={self.header_name: "Invalid API key"},
            )

        return AuthenticatedUser(key_config)

    @property
    def password_sign_in_available(self) -> bool:
        """Whether a person can sign in with a username and password here."""
        return self.ldap is not None or self.users is not None

    def password_sign_in(self, username: str, password: str, client: str = "") -> dict[str, Any]:
        """Sign a person in with a password: a token this server accepts.

        A local user (made in the console) is checked first, and only against
        the local users: a name that exists there is never tried at the
        directory too. Any other name goes to the directory, when there is one.

        Blocking (it hashes, or talks to the directory); call it off the event loop.

        Raises:
            SignInRefused: Wrong username or password, or not allowed to sign in.
            TooManyFailures: Paused after repeated failures.
            DirectoryUnreachable: The directory did not answer.
        """
        assert self.sessions is not None
        if self.users is not None and self.users.get(username.strip()) is not None:
            return self._local_sign_in(username, password, client)
        if self.ldap is None:
            if self.users is None:
                raise SignInRefused(REFUSED)
            return self._local_sign_in(username, password, client)  # counts, then refuses
        try:
            person = self.ldap.authenticate(username, password, client)
        except TooManyFailures:
            logger.warning("Sign-in paused for %r after repeated failures", username[:64])
            raise
        except SignInRefused:
            logger.warning("Sign-in refused for %r", username[:64])
            raise
        roles = self.ldap.roles(person.groups)
        logger.info("Signed in %s (roles: %s)", person.username, ", ".join(roles) or "none")
        return self.sessions.issue(
            person.username, roles, person.tenant, display=person.display, extra={"src": "ldap"}
        )

    def _local_sign_in(self, username: str, password: str, client: str) -> dict[str, Any]:
        assert self.users is not None and self.sessions is not None
        try:
            user = self.users.authenticate(username, password, client)
        except TooManyFailures:
            logger.warning("Sign-in paused for %r after repeated failures", username[:64])
            raise
        except SignInRefused:
            logger.warning("Sign-in refused for %r", username[:64])
            raise
        logger.info("Signed in %s (local user)", user.name)
        return self.sessions.issue(
            user.name,
            list(user.roles),
            user.tenant,
            display=user.display,
            ttl_seconds=self.users.config.session_ttl_seconds,
            extra={"src": "local", "pv": password_version(user)},
        )

    @property
    def accepts_bearer(self) -> bool:
        """Whether a bearer token can sign anyone in: an identity provider or a directory."""
        return self.oidc is not None or self.sessions is not None

    def _checked(self, caller: ApiKeyConfig) -> AuthenticatedUser:
        """The user a sign-in names, refused now if a row rule needs what the caller lacks.

        A row rule that needs a claim the sign-in lacks is refused here, once,
        rather than as an error on every table the caller later reads.
        """
        user = AuthenticatedUser(caller)
        try:
            for table in caller.row_filters:
                user.row_policy.conditions_for(table)
        except PolicyError as e:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail=f"Signed in, but refused: {e}"
            ) from e
        return user

    def _session_user(self, request: Request, token: str) -> AuthenticatedUser:
        """The person a token this server issued names, with what they may do *now*."""
        assert self.sessions is not None
        try:
            claims = self.sessions.verify(token)
        except SessionError as e:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(e),
                headers=self._challenge(request, error=str(e)),
            ) from e
        if claims.get("src") == "local":
            return self._checked(self._local_caller(request, claims))
        if self.ldap is None:
            self._refuse_session(request, "The token is not valid")
        assert self.ldap is not None
        roles = [str(r) for r in claims.get("roles", []) if isinstance(r, str)]
        tenant = claims.get("tenant")
        return self._checked(
            self.ldap.caller(str(claims["sub"]), roles, str(tenant) if tenant else None)
        )

    def _local_caller(self, request: Request, claims: dict[str, Any]) -> ApiKeyConfig:
        """A local user's token, good only while the account is as it was at sign-in.

        The account is read now, not from the token: disabling or removing a user,
        or changing their permissions, applies to a sign-in already made. A
        changed password ends it.
        """
        if self.users is None:
            self._refuse_session(request, "The token is not valid")
        assert self.users is not None
        name = str(claims["sub"])
        user = self.users.get(name)
        if user is None or user.disabled:
            self._refuse_session(request, "This account is no longer active; sign in again")
        assert user is not None
        if claims.get("pv") != password_version(user):
            self._refuse_session(request, "The password changed; sign in again")
        caller = self.users.caller_for(name)
        assert caller is not None
        return caller

    def _refuse_session(self, request: Request, reason: str) -> NoReturn:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=reason,
            headers=self._challenge(request, error=reason),
        )

    async def _bearer_user(self, request: Request, token: str) -> AuthenticatedUser:
        """The caller a provider's (or this server's own) token names, once it checks out."""
        if self.sessions is not None and is_session_token(token):
            return self._session_user(request, token)
        if self.oidc is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="The token is not valid",
                headers=self._challenge(request, error="The token is not valid"),
            )
        try:
            # Off the event loop: the provider's keys may have to be fetched.
            claims = await asyncio.to_thread(self.oidc.verify, token)
        except ProviderUnreachable as e:
            # Not the token's fault: say so, rather than tell a client to sign in again.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=str(e),
                headers={"Retry-After": "10"},
            ) from e
        except TokenError as e:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(e),
                headers=self._challenge(request, error=str(e)),
            ) from e
        return self._checked(self.oidc.caller(claims))

    def _challenge(self, request: Request, error: str | None = None) -> dict[str, str]:
        """The 401's headers: the API-key note, and with OIDC where to find the provider.

        ``resource_metadata`` (RFC 9728) is how an MCP client learns which
        authorization server to send its user to.
        """
        headers = {self.header_name: "API key is missing" if error is None else "Invalid"}
        if self.oidc is None and self.sessions is not None:
            said = (error or "").replace('"', "'")
            headers["WWW-Authenticate"] = 'Bearer realm="truejoin"' + (
                f', error="invalid_token", error_description="{said}"' if error else ""
            )
        if self.oidc is not None:
            # The configured public URL when there is one: behind a proxy the
            # request's own base is an internal address a client cannot reach.
            base = (self.oidc.config.resource_url or str(request.base_url)).rstrip("/")
            parts = [
                'Bearer realm="truejoin"',
                f'resource_metadata="{base}/.well-known/oauth-protected-resource"',
            ]
            if error is not None:
                said = error.replace('"', "'")
                parts += ['error="invalid_token"', f'error_description="{said}"']
            headers["WWW-Authenticate"] = ", ".join(parts)
        return headers

    @staticmethod
    def _remember(request: Request, user: AuthenticatedUser | None) -> AuthenticatedUser | None:
        """Record who is calling on the request, then hand the user back.

        Routes register auth as ``dependencies=[Depends(...)]``, and FastAPI
        throws away what such a dependency returns — so without this the
        identity built here would never reach a handler. Stashing it on
        ``request.state`` gives every route access to the caller without
        changing a single signature.

        ``None`` means "nobody was authenticated", which happens when auth is
        off or the path is public. Anything that filters or masks by identity
        must treat that as *unknown*, not as *permitted*.
        """
        request.state.user = user
        # Who the REST paths' statements run for, as the database is told when
        # it asks (options.session_identity). Set on the request's own task, so
        # it ends with the request.
        CURRENT_IDENTITY.set(
            None
            if user is None
            else SessionIdentity(actor=user.name, tenant=user.tenant, roles=tuple(user.roles))
        )
        return user

    def require(self, permission: Permission) -> Callable[..., Awaitable[AuthenticatedUser | None]]:
        """Create a dependency that requires a specific permission.

        Usage:
            @router.get("/items", dependencies=[Depends(auth.require(Permission.READ))])
        """

        async def permission_checker(
            request: Request, api_key: str | None = Depends(self.api_key_header)
        ) -> AuthenticatedUser | None:
            # If auth is disabled, allow all
            if not self.enabled:
                return self._remember(request, None)

            # Check if path is public
            if self._is_public_path(request.url.path):
                return self._remember(request, None)

            # Get current user
            user = await self.get_current_user(request, api_key)

            if user is None:
                # Auth disabled or public path
                return self._remember(request, None)

            # Check permission
            if not user.has_permission(permission):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Permission denied: '{permission.value}' required",
                )

            return self._remember(request, user)

        return permission_checker

    def require_any(
        self, permissions: list[Permission]
    ) -> Callable[..., Awaitable[AuthenticatedUser | None]]:
        """Create a dependency that requires any of the specified permissions.

        Usage:
            @router.put("/items/{id}", dependencies=[Depends(auth.require_any([Permission.UPDATE, Permission.ALL]))])
        """

        async def permission_checker(
            request: Request, api_key: str | None = Depends(self.api_key_header)
        ) -> AuthenticatedUser | None:
            if not self.enabled:
                return self._remember(request, None)

            if self._is_public_path(request.url.path):
                return self._remember(request, None)

            user = await self.get_current_user(request, api_key)

            if user is None:
                return self._remember(request, None)

            # Check if user has any of the required permissions
            for permission in permissions:
                if user.has_permission(permission):
                    return self._remember(request, user)

            perm_names = [p.value for p in permissions]
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Permission denied: one of {perm_names} required",
            )

        return permission_checker


def roles_of(request: Request) -> list[str]:
    """The roles recorded for this request (empty when nobody is identified)."""
    caller = caller_of(request)
    return caller.roles if caller is not None else []


def policy_of(request: Request) -> RowPolicy:
    """The row policy recorded for this request.

    An unauthenticated request has no policy, which is why a configured row
    rule also requires authentication to be on (see
    ``validate_production_config``).
    """
    caller = caller_of(request)
    return caller.row_policy if caller is not None else EMPTY_POLICY


def caller_of(request: Request) -> AuthenticatedUser | None:
    """The authenticated caller recorded for this request, if any.

    ``None`` means nobody was authenticated — auth is disabled, or the path is
    public. It never means "permitted": a caller that filters or masks rows by
    identity has to decide what to do about an unknown one, and the safe
    answer is to refuse rather than to serve everything.
    """
    return getattr(request.state, "user", None)
