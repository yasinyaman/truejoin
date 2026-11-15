"""Signing in with a directory password: who is let in, who is not, and what the token says.

The directory is ldap3's in-memory one, so every refusal below comes from the
same protocol answers (invalid credentials, no such entry) a real server gives.
"""

import time

import jwt
import pytest
from fastapi.testclient import TestClient
from ldap3 import MOCK_SYNC, Connection, Server

from truejoin.adapters.inbound.http.auth import AuthManager, Permission
from truejoin.adapters.inbound.http.ldap import (
    REFUSED,
    DirectoryUnreachable,
    LdapAuthenticator,
    LoginThrottle,
    SignInRefused,
    TooManyFailures,
)
from truejoin.adapters.inbound.http.sessions import (
    SessionError,
    SessionTokens,
    is_session_token,
    signing_key,
)
from truejoin.application.settings import (
    AuthConfig,
    LdapConfig,
    Settings,
    validate_production_config,
)

BASE = "dc=acme,dc=com"
SERVICE = f"uid=search,ou=people,{BASE}"
PEOPLE = f"ou=people,{BASE}"


def settings(**overrides) -> LdapConfig:
    return LdapConfig(
        **{
            "preset": "lldap",
            "url": "ldap://directory.test",
            "bind_dn": SERVICE,
            "bind_password": "search-secret",
            "user_base": PEOPLE,
            "group_roles": {"analysts": ["analyst"], "stewards": ["steward"]},
            "role_permissions": {"analyst": ["read", "query"], "steward": ["update"]},
            **overrides,
        }
    )


class Directory:
    """An in-memory directory that remembers every account that was bound as."""

    def __init__(self) -> None:
        self.server = Server("ldap://directory.test")
        self.bound: list[str] = []
        self.options: list[dict] = []
        self._seed = self._connect(user=SERVICE, password="search-secret")
        self.add(SERVICE, uid="search", password="search-secret", cn="search")
        self.add(
            f"uid=ayse,{PEOPLE}",
            uid="ayse",
            password="correct horse",
            cn="Ayşe Yılmaz",
            displayName="Ayşe Yılmaz",
            mail="ayse@acme.com",
            memberOf=[f"cn=analysts,ou=groups,{BASE}", f"cn=everyone,ou=groups,{BASE}"],
        )
        self.add(
            f"uid=mehmet,{PEOPLE}",
            uid="mehmet",
            password="s3cret",
            cn="Mehmet Demir",
            memberOf=[f"cn=stewards,ou=groups,{BASE}"],
        )
        self.add(f"uid=ali,{PEOPLE}", uid="ali", password="pw", cn="Ali", memberOf=[])

    def _connect(self, **kw) -> Connection:
        return Connection(self.server, client_strategy=MOCK_SYNC, **kw)

    def add(self, dn: str, password: str, **attributes) -> None:
        self._seed.strategy.add_entry(
            dn,
            {"objectClass": ["person", "inetOrgPerson"], "userPassword": password, **attributes},
        )

    def __call__(self, server, **kw) -> Connection:
        # The entries live on this directory's own server object, not on the one
        # the authenticator built to describe the same address.
        self.bound.append(kw.get("user", ""))
        self.options.append(kw)
        return Connection(self.server, client_strategy=MOCK_SYNC, **kw)

    def binds_as(self, dn: str) -> int:
        return self.bound.count(dn)


@pytest.fixture
def directory() -> Directory:
    return Directory()


def authenticator(directory, **overrides) -> LdapAuthenticator:
    return LdapAuthenticator(settings(**overrides), connection_factory=directory)


class TestSignIn:
    def test_the_right_password_names_the_person_and_their_groups(self, directory):
        who = authenticator(directory).authenticate("ayse", "correct horse")
        assert who.username == "ayse"
        assert who.display == "Ayşe Yılmaz" and who.email == "ayse@acme.com"
        assert who.groups == ("analysts", "everyone")

    def test_a_wrong_password_and_an_unknown_user_are_told_the_same(self, directory):
        auth = authenticator(directory)
        with pytest.raises(SignInRefused) as wrong:
            auth.authenticate("ayse", "wrong")
        with pytest.raises(SignInRefused) as unknown:
            auth.authenticate("nobody", "whatever")
        assert str(wrong.value) == str(unknown.value) == REFUSED

    @pytest.mark.parametrize("password", ["", None])
    def test_an_empty_password_is_refused_before_anything_binds_as_the_user(
        self, directory, password
    ):
        # Many servers answer success to a bind with a name and no password.
        with pytest.raises(SignInRefused):
            authenticator(directory).authenticate("ayse", password)
        assert directory.binds_as(f"uid=ayse,{PEOPLE}") == 0
        assert directory.bound == []  # not even the search account was used

    @pytest.mark.parametrize(
        "username", ["*", "a*", "ayse)(uid=*", "ayse)(|(uid=*", "*)(uid=*))(|(uid=*", "\x00ayse"]
    )
    def test_filter_characters_in_the_username_widen_nothing(self, directory, username):
        with pytest.raises(SignInRefused):
            authenticator(directory).authenticate(username, "correct horse")

    def test_two_entries_for_one_username_is_a_refusal_not_a_guess(self, directory):
        directory.add(f"uid=ayse2,{PEOPLE}", uid="ayse", password="other", cn="Other Ayşe")
        with pytest.raises(SignInRefused):
            authenticator(directory).authenticate("ayse", "correct horse")

    def test_a_very_long_username_is_not_sent_to_the_directory(self, directory):
        with pytest.raises(SignInRefused):
            authenticator(directory).authenticate("a" * 300, "x")
        assert directory.bound == []

    def test_only_the_allowed_groups_may_sign_in(self, directory):
        auth = authenticator(directory, allowed_groups=["Analysts"])  # case does not matter
        assert auth.authenticate("ayse", "correct horse").username == "ayse"
        with pytest.raises(SignInRefused, match="may not sign in"):
            auth.authenticate("mehmet", "s3cret")
        with pytest.raises(SignInRefused, match="may not sign in"):
            auth.authenticate("ali", "pw")

    def test_groups_can_be_searched_instead_of_read_from_the_user(self, directory):
        directory._seed.strategy.add_entry(
            f"cn=analysts,ou=groups,{BASE}",
            {
                "objectClass": ["groupOfNames"],
                "cn": "analysts",
                "member": [f"uid=ayse,{PEOPLE}"],
            },
        )
        auth = authenticator(
            directory,
            preset="openldap",
            user_filter="(uid={username})",
            group_base=f"ou=groups,{BASE}",
        )
        assert auth.authenticate("ayse", "correct horse").groups == ("analysts",)

    def test_the_receive_timeout_is_whole_seconds(self, directory):
        # ldap3 packs it into a socket option and fails on a float, but only
        # against a real socket: the in-memory directory never notices.
        authenticator(directory, timeout_seconds=2.5).authenticate("ayse", "correct horse")
        timeouts = {kw["receive_timeout"] for kw in directory.options}
        assert timeouts == {3} and all(isinstance(t, int) for t in timeouts)

    def test_an_unreachable_directory_is_not_a_wrong_password(self):
        def refuses(server, **kw):
            raise OSError("connection refused")

        auth = LdapAuthenticator(settings(), connection_factory=refuses)
        with pytest.raises(DirectoryUnreachable):
            auth.authenticate("ayse", "correct horse")


class TestRoles:
    def test_groups_become_roles_and_an_unmapped_group_keeps_its_name(self, directory):
        auth = authenticator(directory)
        assert auth.roles(["analysts", "everyone"]) == ["analyst", "everyone"]

    def test_the_caller_has_what_its_roles_allow(self, directory):
        caller = authenticator(directory).caller("ayse", ["analyst", "steward"], "a")
        assert sorted(caller.permissions) == ["query", "read", "update"]
        assert caller.tenant == "a" and caller.key == ""

    def test_an_exempt_role_drops_the_row_rules(self, directory):
        auth = authenticator(
            directory,
            row_filters={"orders": [{"column": "tenant", "value": "${tenant}"}]},
            row_filter_exempt_roles=["dpo"],
        )
        assert auth.caller("a", ["analyst"], "t").row_filters
        assert not auth.caller("b", ["dpo"], "t").row_filters


class TestThrottle:
    def test_repeated_failures_pause_that_user_only(self, directory):
        auth = authenticator(directory, max_failures=3)
        for _ in range(3):
            with pytest.raises(SignInRefused):
                auth.authenticate("ayse", "wrong")
        with pytest.raises(TooManyFailures) as paused:
            auth.authenticate("ayse", "correct horse")  # even the right one waits
        assert paused.value.retry_after > 0
        assert auth.authenticate("mehmet", "s3cret").username == "mehmet"

    def test_a_pause_costs_the_directory_nothing(self, directory):
        auth = authenticator(directory, max_failures=1)
        with pytest.raises(SignInRefused):
            auth.authenticate("ayse", "wrong")
        before = len(directory.bound)
        with pytest.raises(TooManyFailures):
            auth.authenticate("ayse", "wrong again")
        assert len(directory.bound) == before

    def test_the_pause_ends(self, directory):
        now = [1000.0]
        auth = LdapAuthenticator(
            settings(max_failures=1, lockout_seconds=60),
            clock=lambda: now[0],
            connection_factory=directory,
        )
        with pytest.raises(SignInRefused):
            auth.authenticate("ayse", "wrong")
        with pytest.raises(TooManyFailures):
            auth.authenticate("ayse", "correct horse")
        now[0] += 61
        assert auth.authenticate("ayse", "correct horse").username == "ayse"

    def test_a_success_clears_that_users_count(self, directory):
        auth = authenticator(directory, max_failures=3)
        for _ in range(2):
            with pytest.raises(SignInRefused):
                auth.authenticate("ayse", "wrong")
        auth.authenticate("ayse", "correct horse")
        for _ in range(2):
            with pytest.raises(SignInRefused):
                auth.authenticate("ayse", "wrong")
        assert auth.authenticate("ayse", "correct horse")

    def test_one_address_trying_many_usernames_is_paused_eventually(self, directory):
        auth = authenticator(directory, max_failures=1)  # an address tolerates ten times that
        for n in range(10):
            with pytest.raises(SignInRefused):
                auth.authenticate(f"user{n}", "guess", client="10.0.0.9")
        with pytest.raises(TooManyFailures):
            auth.authenticate("ayse", "correct horse", client="10.0.0.9")
        assert auth.authenticate("ayse", "correct horse", client="10.0.0.10")

    def test_the_window_slides(self):
        now = [0.0]
        throttle = LoginThrottle(2, 100, clock=lambda: now[0])
        throttle.failed("k")
        now[0] = 60
        throttle.failed("k")
        with pytest.raises(TooManyFailures):
            throttle.check("k")
        now[0] = 101  # the first failure has aged out; one is left
        throttle.check("k")


class TestSessionTokens:
    KEY = signing_key("a-secret-for-tests")

    def tokens(self, ttl=3600) -> SessionTokens:
        return SessionTokens(ttl, key=self.KEY)

    def test_what_is_issued_is_accepted(self):
        issued = self.tokens().issue("ayse", ["analyst"], "a", display="Ayşe")
        claims = self.tokens().verify(issued["access_token"])
        assert claims["sub"] == "ayse" and claims["roles"] == ["analyst"]
        assert claims["tenant"] == "a" and issued["name"] == "Ayşe"
        assert issued["expires_at"] == claims["exp"]

    def test_an_expired_token_says_to_sign_in_again(self):
        issued = self.tokens(ttl=60).issue("ayse", [], now=time.time() - 3600)
        with pytest.raises(SessionError, match="sign in again"):
            self.tokens().verify(issued["access_token"])

    def test_a_token_from_another_key_is_refused(self):
        other = SessionTokens(60, key=signing_key("another-secret"))
        with pytest.raises(SessionError):
            self.tokens().verify(other.issue("ayse", [])["access_token"])

    def test_the_server_secret_is_not_the_signing_key(self):
        assert signing_key("a-secret-for-tests") != b"a-secret-for-tests"
        assert signing_key("a-secret-for-tests") == signing_key("a-secret-for-tests")

    @pytest.mark.filterwarnings("ignore::jwt.warnings.InsecureKeyLengthWarning")
    @pytest.mark.parametrize("algorithm", ["none", "HS512"])
    def test_other_algorithms_are_refused(self, algorithm):
        body = {"iss": "truejoin", "sub": "root", "iat": 1, "exp": int(time.time()) + 600}
        forged = jwt.encode(body, None if algorithm == "none" else self.KEY, algorithm=algorithm)
        with pytest.raises(SessionError):
            self.tokens().verify(forged)

    def test_another_issuer_is_refused(self):
        body = {"iss": "someone", "sub": "root", "iat": 1, "exp": int(time.time()) + 600}
        with pytest.raises(SessionError):
            self.tokens().verify(jwt.encode(body, self.KEY, algorithm="HS256"))

    def test_a_provider_token_is_not_taken_for_ours(self):
        from cryptography.hazmat.primitives.asymmetric import rsa

        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        provider = jwt.encode({"sub": "x"}, private, algorithm="RS256")
        assert not is_session_token(provider)
        assert is_session_token(self.tokens().issue("ayse", [])["access_token"])
        assert not is_session_token("not a token")


def manager(directory, **overrides) -> AuthManager:
    config = AuthConfig(enabled=True, ldap=settings(**overrides))
    return AuthManager(
        config,
        ldap=LdapAuthenticator(config.ldap, connection_factory=directory),
        sessions=SessionTokens(config.ldap.session_ttl_seconds, key=TestSessionTokens.KEY),
    )


class Request:
    """The part of a request AuthManager reads."""

    def __init__(self, token: str | None = None, path: str = "/x") -> None:
        from starlette.requests import Request as Starlette

        headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
        self.scope = {
            "type": "http",
            "headers": headers,
            "path": path,
            "scheme": "http",
            "server": ("testserver", 80),
            "query_string": b"",
        }
        self.request = Starlette(self.scope)


class TestAuthManager:
    async def test_the_token_a_sign_in_gives_is_accepted_as_a_bearer_token(self, directory):
        auth = manager(directory)
        signed = auth.password_sign_in("ayse", "correct horse")
        user = await auth.get_current_user(Request(signed["access_token"]).request)
        assert user is not None and user.name == "ayse"
        assert user.has_permission(Permission.READ) and user.has_permission(Permission.QUERY)
        assert not user.has_permission(Permission.UPDATE)
        assert user.roles == ["analyst", "everyone"]

    async def test_what_a_role_may_do_is_read_when_the_token_is_used(self, directory):
        auth = manager(directory)
        token = auth.password_sign_in("ayse", "correct horse")["access_token"]
        auth.ldap.config.role_permissions["analyst"] = ["read"]  # narrowed after sign-in
        user = await auth.get_current_user(Request(token).request)
        assert user is not None and not user.has_permission(Permission.QUERY)

    async def test_a_token_that_is_not_ours_is_a_401_that_says_to_sign_in(self, directory):
        from fastapi import HTTPException

        auth = manager(directory)
        forged = jwt.encode(
            {"sub": "root", "iss": "truejoin", "exp": 9999999999, "iat": 1},
            b"x" * 32,
            algorithm="HS256",
        )
        with pytest.raises(HTTPException) as refused:
            await auth.get_current_user(Request(forged).request)
        assert refused.value.status_code == 401
        assert "Bearer" in refused.value.headers["WWW-Authenticate"]

    async def test_no_credentials_is_a_401(self, directory):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as refused:
            await manager(directory).get_current_user(Request().request)
        assert refused.value.status_code == 401
        assert "bearer token" in refused.value.detail

    async def test_api_keys_still_work_beside_the_directory(self, directory):
        from truejoin.application.settings import ApiKeyConfig

        config = AuthConfig(
            enabled=True,
            ldap=settings(),
            api_keys=[ApiKeyConfig(key="k" * 32, name="robot", permissions=["read"])],
        )
        auth = AuthManager(
            config, ldap=LdapAuthenticator(config.ldap, connection_factory=directory)
        )
        scope = Request().request
        user = await auth.get_current_user(scope, api_key="k" * 32)
        assert user is not None and user.name == "robot"


class TestConsole:
    def client(self, directory, **overrides) -> TestClient:
        from truejoin.adapters.inbound.http.app import create_app, runtime_of
        from truejoin.application.settings import RuntimeEnv

        app = create_app(env=RuntimeEnv())
        runtime_of(app).auth_manager = manager(directory, **overrides)
        return TestClient(app)

    def test_the_console_is_told_a_password_sign_in_exists(self, directory):
        assert self.client(directory).get("/ui/sign-in").json() == {
            "enabled": False,
            "password": True,
        }

    def test_without_a_directory_the_console_is_told_nothing_new(self):
        from truejoin.adapters.inbound.http.app import create_app, runtime_of
        from truejoin.application.settings import RuntimeEnv

        app = create_app(env=RuntimeEnv())
        runtime_of(app).auth_manager = AuthManager(AuthConfig(enabled=True))
        client = TestClient(app)
        assert client.get("/ui/sign-in").json() == {"enabled": False}
        assert (
            client.post("/ui/sign-in/password", json={"username": "a", "password": "b"}).status_code
            == 404
        )

    def test_a_good_sign_in_gives_a_token_the_console_can_use(self, directory):
        client = self.client(directory)
        r = client.post(
            "/ui/sign-in/password", json={"username": "ayse", "password": "correct horse"}
        )
        assert r.status_code == 200
        body = r.json()
        assert body["name"] == "Ayşe Yılmaz" and body["access_token"].count(".") == 2
        # The page then asks what this caller may do, with the token as a bearer token.
        access = client.get(
            "/ui/access", headers={"Authorization": f"Bearer {body['access_token']}"}
        )
        assert access.status_code == 200
        assert access.json() | {"actor": None} == {
            "auth_enabled": True,
            "actor": None,
            "read": True,
            "query": True,
            "update": False,
            "create": False,
            "audit": False,
            "admin": False,
        }
        assert access.json()["actor"] == "ayse"

    def test_a_wrong_password_is_a_401_that_names_no_one(self, directory):
        r = TestClient  # noqa: F841 - readability: the client is built below
        client = self.client(directory)
        for username in ("ayse", "nobody"):
            answer = client.post(
                "/ui/sign-in/password", json={"username": username, "password": "no"}
            )
            assert answer.status_code == 401 and answer.json()["detail"] == REFUSED

    def test_repeated_failures_are_a_429_with_a_retry_time(self, directory):
        client = self.client(directory, max_failures=2)
        for _ in range(2):
            client.post("/ui/sign-in/password", json={"username": "ayse", "password": "no"})
        paused = client.post(
            "/ui/sign-in/password", json={"username": "ayse", "password": "correct horse"}
        )
        assert paused.status_code == 429 and int(paused.headers["Retry-After"]) > 0

    def test_an_empty_form_is_rejected_by_the_request_model(self, directory):
        client = self.client(directory)
        assert (
            client.post(
                "/ui/sign-in/password", json={"username": "ayse", "password": ""}
            ).status_code
            == 422
        )

    def test_an_unreachable_directory_is_a_502(self):
        def refuses(server, **kw):
            raise OSError("down")

        from truejoin.adapters.inbound.http.app import create_app, runtime_of
        from truejoin.application.settings import RuntimeEnv

        config = AuthConfig(enabled=True, ldap=settings())
        app = create_app(env=RuntimeEnv())
        runtime_of(app).auth_manager = AuthManager(
            config, ldap=LdapAuthenticator(config.ldap, connection_factory=refuses)
        )
        r = TestClient(app).post("/ui/sign-in/password", json={"username": "a", "password": "b"})
        assert r.status_code == 502


class TestConfiguration:
    def test_presets_fill_what_the_file_leaves_out_and_yield_to_what_it_says(self):
        lldap = settings()
        assert lldap.member_of_attr == "memberOf" and "(uid={username})" in lldap.user_filter
        ad = settings(preset="active_directory", user_filter="(sAMAccountName={username})")
        assert ad.username_attr == "sAMAccountName"
        assert ad.user_filter == "(sAMAccountName={username})"

    def test_an_unknown_preset_is_refused(self):
        with pytest.raises(ValueError, match="preset"):
            settings(preset="novell")

    def test_production_refuses_a_plain_ldap_url_and_a_missing_secret(self, monkeypatch):
        monkeypatch.delenv("TRUEJOIN_SECRET_KEY", raising=False)
        cfg = Settings(
            databases=[], settings={"auth": {"enabled": True, "ldap": settings().model_dump()}}
        )
        problems = validate_production_config(cfg, "production")
        assert any("unencrypted" in p for p in problems)
        assert any("TRUEJOIN_SECRET_KEY" in p for p in problems)

    def test_production_accepts_ldaps_with_a_secret(self, monkeypatch):
        monkeypatch.setenv("TRUEJOIN_SECRET_KEY", "x" * 44)
        ldap = settings(url="ldaps://directory.test:636").model_dump()
        cfg = Settings(databases=[], settings={"auth": {"enabled": True, "ldap": ldap}})
        assert not [p for p in validate_production_config(cfg, "production") if "ldap" in p.lower()]

    def test_start_tls_makes_plain_ldap_acceptable(self, monkeypatch):
        monkeypatch.setenv("TRUEJOIN_SECRET_KEY", "x" * 44)
        ldap = settings(start_tls=True).model_dump()
        cfg = Settings(databases=[], settings={"auth": {"enabled": True, "ldap": ldap}})
        assert not [p for p in validate_production_config(cfg, "production") if "unencrypted" in p]
