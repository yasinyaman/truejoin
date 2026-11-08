"""Bearer tokens from an OpenID Connect provider: what is accepted, and who the caller is.

Tokens are signed with a real RSA key and checked against its JWKS, served
by a fake fetcher, so every refusal below is one a real provider's token
would meet.
"""

import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from trio.adapters.inbound.http.auth import AuthManager, Permission, caller_of
from trio.adapters.inbound.http.oidc import OidcVerifier, TokenError, claim
from trio.application.settings import (
    ApiKeyConfig,
    AuthConfig,
    OidcConfig,
    Settings,
    validate_production_config,
)

ISSUER = "https://login.example.com/realms/acme"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def jwk(private, kid: str) -> dict:
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    return {**public, "kid": kid, "use": "sig", "alg": "RS256"}


class Provider:
    """Discovery and keys, counting how often they are fetched."""

    def __init__(self) -> None:
        self.keys = [jwk(KEY, "k1")]
        self.fetched: list[str] = []

    def __call__(self, url: str) -> dict:
        self.fetched.append(url)
        if url.endswith("/.well-known/openid-configuration"):
            return {
                "issuer": ISSUER,
                "jwks_uri": ISSUER + "/certs",
                "authorization_endpoint": ISSUER + "/auth",
                "token_endpoint": ISSUER + "/token",
            }
        assert url == ISSUER + "/certs"
        return {"keys": list(self.keys)}


def config(**overrides) -> OidcConfig:
    return OidcConfig(
        issuer=ISSUER,
        audience="trio",
        roles_claim="realm_access.roles",
        tenant_claim="tenant",
        role_permissions={"analyst": ["read", "query"], "steward": ["update"]},
        row_filters={"orders": [{"column": "tenant", "value": "${tenant}"}]},
        row_filter_exempt_roles=["dpo"],
        **overrides,
    )


def token(private=KEY, kid="k1", alg="RS256", **claims) -> str:
    now = int(time.time())
    body = {
        "iss": ISSUER,
        "aud": "trio",
        "sub": "u-1",
        "preferred_username": "ayse",
        "tenant": "a",
        "realm_access": {"roles": ["analyst"]},
        "iat": now,
        "exp": now + 300,
        **claims,
    }
    return jwt.encode(body, private, algorithm=alg, headers={"kid": kid})


@pytest.fixture
def provider() -> Provider:
    return Provider()


@pytest.fixture
def verifier(provider) -> OidcVerifier:
    return OidcVerifier(config(), fetch=provider)


class TestVerify:
    def test_a_good_token_and_its_keys_fetched_once(self, verifier, provider):
        assert verifier.verify(token())["preferred_username"] == "ayse"
        verifier.verify(token())
        assert provider.fetched == [
            ISSUER + "/.well-known/openid-configuration",
            ISSUER + "/certs",
        ]

    @pytest.mark.parametrize(
        ("made", "reason"),
        [
            (lambda: token(exp=int(time.time()) - 600), "expired"),
            (lambda: token(aud="someone-else"), "not issued for this server"),
            (lambda: token(iss="https://evil.example.com"), "another provider"),
            (lambda: token(private=OTHER), "not valid"),  # right kid, wrong key
            (lambda: jwt.encode({"sub": "x"}, "s" * 32, algorithm="HS256"), "HS256"),
            (lambda: jwt.encode({"sub": "x"}, None, algorithm="none"), "none"),
            (lambda: "not.a.token", "Not a token"),
        ],
    )
    def test_what_is_refused(self, verifier, made, reason):
        with pytest.raises(TokenError, match=reason):
            verifier.verify(made())

    def test_a_token_without_a_subject_is_refused(self, verifier):
        bare = jwt.decode(token(), options={"verify_signature": False})
        del bare["sub"]
        with pytest.raises(TokenError):
            verifier.verify(jwt.encode(bare, KEY, algorithm="RS256", headers={"kid": "k1"}))

    def test_a_rotated_key_is_fetched_again_but_not_on_every_token(self, verifier, provider):
        verifier.verify(token())
        provider.keys.append(jwk(OTHER, "k2"))
        verifier._fetched_at -= 120  # the last fetch is old enough to repeat
        assert verifier.verify(token(private=OTHER, kid="k2"))["sub"] == "u-1"
        before = len(provider.fetched)
        with pytest.raises(TokenError, match="does not publish"):
            verifier.verify(token(kid="k3"))  # unknown, and fetched a moment ago
        assert len(provider.fetched) == before

    def test_an_unreachable_provider_is_a_refusal_not_a_crash(self):
        def down(url):
            raise OSError("connection refused")

        with pytest.raises(TokenError, match="cannot be read"):
            OidcVerifier(config(), fetch=down).verify(token())


class TestCaller:
    def test_claims_make_the_caller(self, verifier):
        claims = verifier.verify(token(realm_access={"roles": ["analyst", "steward"]}))
        caller = verifier.caller(claims)
        assert caller.name == "ayse" and caller.tenant == "a"
        assert caller.roles == ["analyst", "steward"]
        assert caller.permissions == ["read", "query", "update"]
        assert caller.has_row_rules

    def test_an_exempt_role_has_no_row_rules_and_no_role_no_permissions(self, verifier):
        dpo = verifier.caller(verifier.verify(token(realm_access={"roles": ["dpo"]})))
        assert not dpo.has_row_rules and dpo.permissions == []

    def test_the_subject_names_a_caller_without_a_username(self, verifier):
        claims = verifier.verify(token(preferred_username=None))
        assert verifier.caller({**claims, "preferred_username": None}).name == "u-1"

    def test_a_dotted_claim(self):
        assert claim({"a": {"b": ["x"]}}, "a.b") == ["x"]
        assert claim({"a": 1}, "a.b") is None and claim({}, None) is None


def app_with(verifier: OidcVerifier) -> TestClient:
    auth = AuthManager(
        AuthConfig(
            enabled=True,
            api_keys=[ApiKeyConfig(key="k" * 40, name="key", permissions=["read"])],
            oidc=verifier.config,
        ),
        oidc=verifier,
    )
    app = FastAPI()

    @app.get("/q", dependencies=[Depends(auth.require(Permission.READ))])
    async def q(request: __import__("fastapi").Request) -> dict:
        caller = caller_of(request)
        return {"name": caller.name, "tenant": caller.tenant}

    return TestClient(app)


class TestOverHttp:
    def test_a_bearer_token_or_a_key(self, verifier):
        client = app_with(verifier)
        r = client.get("/q", headers={"Authorization": f"Bearer {token()}"})
        assert r.status_code == 200 and r.json() == {"name": "ayse", "tenant": "a"}
        assert client.get("/q", headers={"X-API-Key": "k" * 40}).json()["name"] == "key"

    def test_permissions_come_from_roles(self, verifier):
        client = app_with(verifier)
        bare = token(realm_access={"roles": ["steward"]})  # update, not read
        assert client.get("/q", headers={"Authorization": f"Bearer {bare}"}).status_code == 403

    def test_a_token_without_the_claim_a_row_rule_needs_is_refused(self, verifier):
        client = app_with(verifier)
        no_tenant = token(tenant=None)
        r = client.get("/q", headers={"Authorization": f"Bearer {no_tenant}"})
        assert r.status_code == 403 and "'tenant'" in r.json()["detail"]
        exempt = token(tenant=None, realm_access={"roles": ["dpo", "analyst"]})
        assert client.get("/q", headers={"Authorization": f"Bearer {exempt}"}).status_code == 200

    def test_a_401_says_where_the_provider_is(self, verifier):
        client = app_with(verifier)
        missing = client.get("/q")
        assert missing.status_code == 401
        challenge = missing.headers["www-authenticate"]
        assert 'resource_metadata="http://testserver/.well-known/oauth-protected-resource"' in (
            challenge
        )
        expired = client.get(
            "/q", headers={"Authorization": f"Bearer {token(exp=int(time.time()) - 600)}"}
        )
        assert (
            expired.status_code == 401
            and 'error="invalid_token"' in (expired.headers["www-authenticate"])
        )
        assert expired.json()["detail"] == "The token has expired"

    def test_without_oidc_a_bearer_token_is_no_key(self):
        auth = AuthManager(AuthConfig(enabled=True, api_keys=[]))
        app = FastAPI()
        app.get("/q", dependencies=[Depends(auth.require(Permission.QUERY))])(lambda: {})
        r = TestClient(app).get("/q", headers={"Authorization": f"Bearer {token()}"})
        assert r.status_code == 401 and "www-authenticate" not in r.headers


def test_the_resource_metadata_names_the_provider(verifier):
    assert verifier.resource_metadata("https://trio.example.com") == {
        "resource": "https://trio.example.com",
        "authorization_servers": [ISSUER],
        "bearer_methods_supported": ["header"],
    }


class TestConfig:
    def test_symmetric_and_unknown_are_refused(self):
        with pytest.raises(ValueError, match="HS256"):
            config(algorithms=["HS256"])
        with pytest.raises(ValueError, match="unknown permission"):
            OidcConfig(issuer=ISSUER, audience="trio", default_permissions=["everything"])

    def test_production_wants_https(self):
        settings = Settings.model_validate(
            {
                "databases": [],
                "settings": {
                    "auth": {
                        "enabled": True,
                        "oidc": {"issuer": "http://login.local", "audience": "trio"},
                    }
                },
            }
        )
        violations = validate_production_config(settings, "production")
        assert any("auth.oidc.issuer is not https" in v for v in violations)


class TokenEndpoint:
    """Answers a code exchange with a token it signs, keeping the forms it got."""

    def __init__(self, answer=None) -> None:
        self.forms: list[dict] = []
        self.answer = answer

    def __call__(self, url: str, form: dict) -> dict:
        assert url == ISSUER + "/token"
        self.forms.append(form)
        return self.answer if self.answer is not None else {"access_token": token()}


VERIFIER = "v" * 43


class TestConsoleSignIn:
    def console(self, provider, endpoint, **overrides) -> TestClient:
        from trio.adapters.inbound.http.app import create_app, runtime_of
        from trio.application.settings import RuntimeEnv

        verifier = OidcVerifier(
            config(console_client_id="trio-console", **overrides), fetch=provider, post=endpoint
        )
        app = create_app(env=RuntimeEnv())
        runtime_of(app).auth_manager = AuthManager(
            AuthConfig(enabled=True, oidc=verifier.config), oidc=verifier
        )
        return TestClient(app)

    def test_the_console_is_told_where_to_send_its_user(self, provider):
        client = self.console(provider, TokenEndpoint())
        assert client.get("/ui/sign-in").json() == {
            "enabled": True,
            "authorization_endpoint": ISSUER + "/auth",
            "client_id": "trio-console",
            "scope": "openid",
            "redirect_uri": "http://testserver/ui",
        }

    def test_the_code_is_exchanged_here_and_the_token_checked(self, provider):
        endpoint = TokenEndpoint()
        client = self.console(provider, endpoint, resource_url="https://trio.example.com/")
        r = client.post("/ui/sign-in/token", json={"code": "c-1", "code_verifier": VERIFIER})
        assert r.status_code == 200
        assert r.json()["name"] == "ayse" and r.json()["access_token"].count(".") == 2
        assert endpoint.forms == [
            {
                "grant_type": "authorization_code",
                "code": "c-1",
                # Back to this server's console only, at its public address.
                "redirect_uri": "https://trio.example.com/ui",
                "client_id": "trio-console",
                "code_verifier": VERIFIER,
            }
        ]

    def test_a_token_this_server_would_not_take_is_not_handed_over(self, provider):
        stranger = TokenEndpoint({"access_token": token(aud="another-app")})
        r = self.console(provider, stranger).post(
            "/ui/sign-in/token", json={"code": "c", "code_verifier": VERIFIER}
        )
        assert r.status_code == 401 and "not issued for this server" in r.json()["detail"]
        empty = TokenEndpoint({"token_type": "bearer"})
        r = self.console(provider, empty).post(
            "/ui/sign-in/token", json={"code": "c", "code_verifier": VERIFIER}
        )
        assert r.status_code == 401 and "no access token" in r.json()["detail"]

    def test_a_short_verifier_or_no_console_client(self, provider):
        client = self.console(provider, TokenEndpoint())
        short = client.post("/ui/sign-in/token", json={"code": "c", "code_verifier": "x"})
        assert short.status_code == 422
        bare = OidcVerifier(config(), fetch=provider, post=TokenEndpoint())
        assert bare.console_sign_in("http://x/ui") == {"enabled": False}
        with pytest.raises(TokenError, match="no client"):
            bare.exchange("c", VERIFIER, "http://x/ui")
        with pytest.raises(TokenError, match="no client"):
            bare.refresh("r")

    def test_the_refresh_token_comes_back_with_the_sign_in(self, provider):
        endpoint = TokenEndpoint({"access_token": token(), "refresh_token": "r-1"})
        r = self.console(provider, endpoint).post(
            "/ui/sign-in/token", json={"code": "c", "code_verifier": VERIFIER}
        )
        assert r.json()["refresh_token"] == "r-1"
        # A provider that gives none: the console signs in again when it expires.
        r = self.console(provider, TokenEndpoint()).post(
            "/ui/sign-in/token", json={"code": "c", "code_verifier": VERIFIER}
        )
        assert "refresh_token" not in r.json()

    def test_a_refresh_brings_a_checked_token_and_the_rotated_refresh_token(self, provider):
        endpoint = TokenEndpoint({"access_token": token(), "refresh_token": "r-2"})
        r = self.console(provider, endpoint).post(
            "/ui/sign-in/refresh", json={"refresh_token": "r-1"}
        )
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "ayse" and body["refresh_token"] == "r-2"
        assert body["access_token"].count(".") == 2 and body["expires_at"] > 0
        assert endpoint.forms == [
            {"grant_type": "refresh_token", "refresh_token": "r-1", "client_id": "trio-console"}
        ]

    def test_a_provider_that_does_not_rotate_keeps_the_old_refresh_token(self, provider):
        r = self.console(provider, TokenEndpoint()).post(
            "/ui/sign-in/refresh", json={"refresh_token": "r-1"}
        )
        assert r.json()["refresh_token"] == "r-1"

    def test_a_refused_refresh_is_a_401(self, provider):
        def revoked(url: str, form: dict) -> dict:
            raise TokenError("The identity provider refused the sign-in: Token is not active")

        r = self.console(provider, revoked).post(
            "/ui/sign-in/refresh", json={"refresh_token": "r-1"}
        )
        assert r.status_code == 401 and "not active" in r.json()["detail"]
        stranger = TokenEndpoint({"access_token": token(aud="another-app")})
        r = self.console(provider, stranger).post(
            "/ui/sign-in/refresh", json={"refresh_token": "r-1"}
        )
        assert r.status_code == 401 and "not issued for this server" in r.json()["detail"]
        empty = self.console(provider, TokenEndpoint()).post(
            "/ui/sign-in/refresh", json={"refresh_token": ""}
        )
        assert empty.status_code == 422

    def test_an_unreachable_provider_is_a_502_not_a_refusal(self, provider):
        def down(url: str, form: dict) -> dict:
            raise OSError("Connection refused")

        # The console keeps its refresh token and tries again on a 502; a 401 ends the session.
        r = self.console(provider, down).post("/ui/sign-in/refresh", json={"refresh_token": "r"})
        assert r.status_code == 502 and "cannot be reached" in r.json()["detail"]
        r = self.console(provider, down).post(
            "/ui/sign-in/token", json={"code": "c", "code_verifier": VERIFIER}
        )
        assert r.status_code == 502

    def test_without_a_provider_the_console_does_not_offer_it(self):
        from trio.adapters.inbound.http.app import create_app
        from trio.application.settings import RuntimeEnv

        client = TestClient(create_app(env=RuntimeEnv()))
        assert client.get("/ui/sign-in").json() == {"enabled": False}
        assert (
            client.post(
                "/ui/sign-in/token", json={"code": "c", "code_verifier": VERIFIER}
            ).status_code
            == 404
        )
        assert client.post("/ui/sign-in/refresh", json={"refresh_token": "r"}).status_code == 404
