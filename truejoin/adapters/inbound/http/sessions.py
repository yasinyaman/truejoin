"""Sign-in tokens this server issues itself, after a directory accepted a password.

A person who signs in with a username and password (``auth.ldap``) receives a
short-lived token signed by this server. It travels as a bearer token like an
identity provider's, so the console, scripts and MCP clients need nothing new.

What the token carries is who the person is and which roles the directory gave
them. What those roles may do is read from the configuration when the token
is *used*, so changing a role's permissions applies to people already signed in.

The signing key is derived from ``TRUEJOIN_SECRET_KEY``: the same on every
worker and across restarts. Without one, a random key lasts as long as the
process (fine for trying it out; ``validate_production_config`` refuses it).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import time
from typing import Any

import jwt

logger = logging.getLogger(__name__)

ISSUER = "truejoin"
ALGORITHM = "HS256"
#: Separates this key from anything else derived from the same secret.
_CONTEXT = b"truejoin-session-token-v1"


class SessionError(Exception):
    """A session token that is not accepted; the message is safe to show the caller."""


def signing_key(secret: str | None = None) -> bytes:
    """The 32-byte key sessions are signed with.

    ``secret`` defaults to ``TRUEJOIN_SECRET_KEY``. The secret is never used
    as the key itself: it also encrypts what the console stores.
    """
    secret = secret if secret is not None else os.environ.get("TRUEJOIN_SECRET_KEY", "")
    if secret:
        return hmac.new(secret.encode("utf-8"), _CONTEXT, hashlib.sha256).digest()
    logger.warning(
        "TRUEJOIN_SECRET_KEY is not set: sign-in sessions are signed with a key that "
        "lasts until this process ends"
    )
    return secrets.token_bytes(32)


class SessionTokens:
    """Issues and checks the tokens people sign in with."""

    def __init__(self, ttl_seconds: int, key: bytes | None = None, leeway_seconds: int = 5) -> None:
        """Keep the lifetime; ``key`` is for tests, the default is :func:`signing_key`."""
        self.ttl_seconds = ttl_seconds
        self._key = key if key is not None else signing_key()
        self._leeway = leeway_seconds

    def issue(
        self,
        name: str,
        roles: list[str],
        tenant: str | None = None,
        *,
        display: str | None = None,
        now: float | None = None,
        ttl_seconds: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """A token for ``name``, and when it ends.

        ``ttl_seconds`` overrides this issuer's lifetime; ``extra`` adds claims
        (which kind of account this is, and the password it was signed in with).
        """
        issued = int(now if now is not None else time.time())
        expires = issued + (ttl_seconds if ttl_seconds is not None else self.ttl_seconds)
        claims: dict[str, Any] = {
            "iss": ISSUER,
            "sub": name,
            "iat": issued,
            "exp": expires,
            "jti": secrets.token_urlsafe(8),
            "roles": list(roles),
        }
        if tenant is not None:
            claims["tenant"] = tenant
        if display:
            claims["name"] = display
        if extra:
            claims.update({k: v for k, v in extra.items() if k not in claims})
        token = jwt.encode(claims, self._key, algorithm=ALGORITHM)
        return {"access_token": token, "expires_at": expires, "name": display or name}

    def verify(self, token: str) -> dict[str, Any]:
        """The token's claims, once everything about it checks out.

        Raises:
            SessionError: Why it was refused, in words fit for the caller.
        """
        try:
            return jwt.decode(
                token,
                self._key,
                algorithms=[
                    ALGORITHM
                ],  # nothing else: not "none", not a public key as an HMAC secret
                issuer=ISSUER,
                leeway=self._leeway,
                options={"require": ["exp", "iat", "iss", "sub"]},
            )
        except jwt.ExpiredSignatureError as e:
            raise SessionError("The sign-in has expired; sign in again") from e
        except jwt.PyJWTError as e:
            raise SessionError("The token is not valid") from e


def is_session_token(token: str) -> bool:
    """Whether ``token`` claims to be one of ours (an HS256 JWT), without trusting it.

    An identity provider's tokens are asymmetric, so the algorithm in the
    header tells the two apart; the signature then decides.
    """
    try:
        return jwt.get_unverified_header(token).get("alg") == ALGORITHM
    except jwt.PyJWTError:
        return False
