"""Checking an OpenID Connect provider's bearer tokens, and who they say the caller is.

A token is accepted when the provider's published key verifies its signature
(an asymmetric algorithm from the configured list; never ``none`` or an HMAC),
it was issued by the configured issuer for the configured audience, and it is
inside its lifetime (with a little leeway for clocks). The caller it names is
built as an API key would be: a name, a tenant, roles, the permissions those
roles map to, and row rules with ``${tenant}``/``${username}`` from the claims.

The keys come from the provider's discovery document (``jwks_uri``) unless
configured, are cached, and are fetched again when a token names a key the
cache does not have (the provider rotated), at most once a minute.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import httpx
import jwt

from trio.application.settings import ApiKeyConfig, OidcConfig

logger = logging.getLogger(__name__)

#: How long the signing keys are trusted before they are fetched again.
KEYS_TTL_SECONDS = 300.0
#: The least time between two fetches forced by an unknown key id.
REFETCH_SECONDS = 60.0

Fetch = Callable[[str], dict[str, Any]]
#: POST a form, answer the JSON: how the console's code is exchanged.
Post = Callable[[str, dict[str, str]], dict[str, Any]]


class TokenError(Exception):
    """A bearer token that is not accepted; the message is safe to show the caller."""


class ProviderUnreachable(TokenError):
    """The identity provider did not answer: worth trying again, unlike a refusal."""


def _fetch_json(url: str) -> dict[str, Any]:
    response = httpx.get(url, timeout=10.0, follow_redirects=False)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError(f"{url} did not answer with a JSON object")
    return data


def _post_form(url: str, form: dict[str, str]) -> dict[str, Any]:
    response = httpx.post(url, data=form, timeout=10.0, follow_redirects=False)
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError(f"{url} did not answer with a JSON object")
    if response.status_code >= 500:  # the provider is failing, not refusing
        raise ProviderUnreachable(f"The identity provider failed ({response.status_code})")
    if response.status_code >= 400:
        said = data.get("error_description") or data.get("error") or response.status_code
        raise TokenError(f"The identity provider refused the sign-in: {said}")
    return data


def claim(claims: dict[str, Any], path: str | None) -> Any:
    """The claim at a dotted path ("realm_access.roles"), or None."""
    value: Any = claims
    for part in (path or "").split("."):
        if not part:
            return None
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


class OidcVerifier:
    """Verifies bearer tokens against one provider and turns their claims into a caller."""

    def __init__(
        self, config: OidcConfig, fetch: Fetch = _fetch_json, post: Post = _post_form
    ) -> None:
        """Keep the configuration; nothing is fetched until the first token."""
        self.config = config
        self._fetch = fetch
        self._post = post
        self._lock = threading.Lock()
        self._keys: dict[str, jwt.PyJWK] = {}
        self._fetched_at = 0.0
        self._discovery: dict[str, Any] | None = None

    def discovery(self) -> dict[str, Any]:
        """The provider's discovery document, read once."""
        if self._discovery is None:
            self._discovery = self._fetch(
                self.config.issuer.rstrip("/") + "/.well-known/openid-configuration"
            )
        return self._discovery

    def _jwks_url(self) -> str:
        if self.config.jwks_url:
            return self.config.jwks_url
        url = self.discovery().get("jwks_uri")
        if not isinstance(url, str) or not url:
            raise TokenError("The identity provider publishes no signing keys")
        return url

    def _load_keys(self) -> None:
        jwks = self._fetch(self._jwks_url())
        keys: dict[str, jwt.PyJWK] = {}
        for entry in jwks.get("keys", []):
            if entry.get("use", "sig") != "sig" or "kid" not in entry:
                continue
            try:
                keys[str(entry["kid"])] = jwt.PyJWK(entry)
            except jwt.PyJWTError as e:  # an unsupported key type; the others still serve
                logger.warning("Skipping signing key %s: %s", entry.get("kid"), e)
        self._keys = keys
        self._fetched_at = time.monotonic()

    def _key_for(self, kid: str) -> jwt.PyJWK:
        with self._lock:
            age = time.monotonic() - self._fetched_at
            if (
                not self._keys
                or age > KEYS_TTL_SECONDS
                or (kid not in self._keys and age > REFETCH_SECONDS)
            ):
                try:
                    self._load_keys()
                except TokenError:
                    raise
                except Exception as e:
                    logger.warning("Could not fetch the identity provider's keys: %s", e)
                    if not self._keys:
                        raise ProviderUnreachable(
                            "The identity provider's keys cannot be read"
                        ) from e
            key = self._keys.get(kid)
        if key is None:
            raise TokenError("The token is signed with a key the provider does not publish")
        return key

    def verify(self, token: str) -> dict[str, Any]:
        """The token's claims, once everything about it checks out.

        Raises:
            TokenError: Why it was refused, in words fit for the caller.
        """
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as e:
            raise TokenError("Not a token") from e
        algorithm = header.get("alg")
        if algorithm not in self.config.algorithms:
            raise TokenError(f"Tokens signed with {algorithm} are not accepted")
        key = self._key_for(str(header.get("kid", "")))
        try:
            return jwt.decode(
                token,
                key=key.key,
                algorithms=list(self.config.algorithms),
                audience=self.config.audience,
                issuer=self.config.issuer,
                leeway=self.config.leeway_seconds,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
        except jwt.ExpiredSignatureError as e:
            raise TokenError("The token has expired") from e
        except jwt.InvalidAudienceError as e:
            raise TokenError("The token was not issued for this server") from e
        except jwt.InvalidIssuerError as e:
            raise TokenError("The token was issued by another provider") from e
        except jwt.PyJWTError as e:
            raise TokenError("The token is not valid") from e

    def caller(self, claims: dict[str, Any]) -> ApiKeyConfig:
        """Who the claims say the caller is, in the shape an API key has."""
        config = self.config
        name = claim(claims, config.username_claim) or claims.get("sub")
        roles_value = claim(claims, config.roles_claim)
        roles = [str(r) for r in roles_value] if isinstance(roles_value, list) else []
        if isinstance(roles_value, str):
            roles = roles_value.split()
        tenant = claim(claims, config.tenant_claim)
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
            key="",  # nothing to compare: the signature was the proof
            name=str(name),
            permissions=permissions,
            tenant=str(tenant) if tenant is not None else None,
            roles=roles,
            row_filters={} if exempt else config.row_filters,
        )

    def console_sign_in(self, redirect_uri: str) -> dict[str, Any]:
        """Where the console sends its user, and as which client; disabled without one."""
        if not self.config.console_client_id:
            return {"enabled": False}
        try:
            endpoint = self.discovery().get("authorization_endpoint")
        except Exception as e:
            logger.warning("Could not read the identity provider's discovery document: %s", e)
            return {"enabled": False, "detail": "The identity provider cannot be reached"}
        return {
            "enabled": bool(endpoint),
            "authorization_endpoint": endpoint,
            "client_id": self.config.console_client_id,
            "scope": " ".join(self.config.console_scopes),
            "redirect_uri": redirect_uri,
        }

    def exchange(self, code: str, code_verifier: str, redirect_uri: str) -> dict[str, Any]:
        """The console's authorization code for an access token, checked before it is handed over.

        Done here rather than in the browser so the console's page keeps
        talking to this server only (its CSP) and the provider needs no CORS
        for it. The token must pass the same checks as any bearer token: one
        issued for another audience is refused, not passed on.

        Raises:
            TokenError: The provider refused, or its token would not be accepted.
        """
        return self._grant(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            }
        )

    def refresh(self, refresh_token: str) -> dict[str, Any]:
        """A new access token for the console, from the refresh token its sign-in gave it.

        The provider may rotate the refresh token; the old one is kept when it
        does not send a new one.

        Raises:
            TokenError: The provider refused (expired, revoked, signed out),
                or its token would not be accepted.
        """
        answer = self._grant({"grant_type": "refresh_token", "refresh_token": refresh_token})
        answer.setdefault("refresh_token", refresh_token)
        return answer

    def _grant(self, form: dict[str, str]) -> dict[str, Any]:
        """Ask the token endpoint, as the console's client, and check what comes back."""
        if not self.config.console_client_id:
            raise TokenError("The console has no client at the identity provider")
        try:
            endpoint = self.discovery().get("token_endpoint")
            if not isinstance(endpoint, str) or not endpoint:
                raise TokenError("The identity provider publishes no token endpoint")
            answer = self._post(endpoint, {**form, "client_id": self.config.console_client_id})
        except TokenError:
            raise
        except Exception as e:  # unreachable, timed out, or not JSON
            logger.warning("Could not reach the identity provider's token endpoint: %s", e)
            raise ProviderUnreachable("The identity provider cannot be reached") from e
        token = answer.get("access_token")
        if not isinstance(token, str) or not token:
            raise TokenError("The identity provider gave no access token")
        claims = self.verify(token)
        result = {
            "access_token": token,
            "expires_at": claims["exp"],
            "name": self.caller(claims).name,
        }
        refresh = answer.get("refresh_token")
        if isinstance(refresh, str) and refresh:
            result["refresh_token"] = refresh
        return result

    def resource_metadata(self, resource: str) -> dict[str, Any]:
        """RFC 9728 protected resource metadata: how an MCP client finds the provider."""
        metadata: dict[str, Any] = {
            "resource": self.config.resource_url or resource,
            "authorization_servers": [self.config.issuer],
            "bearer_methods_supported": ["header"],
        }
        if self.config.scopes:
            metadata["scopes_supported"] = self.config.scopes
        return metadata
