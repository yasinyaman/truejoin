"""Local users made from the console: passwords kept as hashes, rules about who may change whom,
and signing in with them.

scrypt is made cheap for these tests (the cost is a constant, checked on its own); every
property below holds at any cost.
"""

import json
import os
import stat
import threading
from datetime import UTC, datetime

import pytest
from click.testing import CliRunner
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient

from truejoin.adapters.inbound.cli.users import users as users_cli
from truejoin.adapters.inbound.http.auth import AuthManager, Permission, caller_of
from truejoin.adapters.inbound.http.routes.users import create_users_router
from truejoin.adapters.inbound.http.sessions import SessionTokens, signing_key
from truejoin.adapters.outbound.user_store.json_file import JsonFileUserStore
from truejoin.application.local_users import LocalUsers, password_version
from truejoin.application.settings import (
    ApiKeyConfig,
    AuthConfig,
    LocalUsersConfig,
    Settings,
    validate_production_config,
)
from truejoin.domain.governance import users as users_domain
from truejoin.domain.governance.keys import Grantor
from truejoin.domain.governance.signin import REFUSED, SignInRefused, TooManyFailures
from truejoin.domain.governance.users import (
    ManagedUser,
    UserError,
    check_may_manage,
    check_name,
    check_password,
    check_user_grant,
    hash_password,
    verify_password,
)

#: Read before the fixture makes scrypt cheap: what the server really uses.
SHIPPED_COST = (users_domain.SCRYPT_N, users_domain.SCRYPT_R, users_domain.SCRYPT_P)
PASSWORD = "correct horse battery"
ROOT = Grantor(name="root", permissions=("all",), roles=(), has_row_rules=False)
LIMITED = Grantor(name="helper", permissions=("read", "admin"), roles=(), has_row_rules=False)
KEY = signing_key("a-secret-for-tests")


@pytest.fixture(autouse=True)
def cheap_scrypt(monkeypatch):
    monkeypatch.setattr(users_domain, "SCRYPT_N", 2**4)


class Clock:
    def __init__(self) -> None:
        self.at = datetime(2026, 11, 15, 9, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.at

    def tick(self, seconds: int = 1) -> None:
        from datetime import timedelta

        self.at += timedelta(seconds=seconds)


def config(**overrides) -> LocalUsersConfig:
    return LocalUsersConfig(store="unused.json", min_password_length=10, **overrides)


def service(tmp_path, clock=None, **overrides) -> LocalUsers:
    return LocalUsers(
        JsonFileUserStore(tmp_path / "users.json"),
        config(**overrides),
        reserved=["service-key"],
        **({"clock": clock} if clock else {}),
    )


def make(svc: LocalUsers, name="ayse", permissions=("read",), grantor=ROOT, **kw) -> ManagedUser:
    return svc.create(
        grantor,
        name=name,
        password=kw.pop("password", PASSWORD),
        permissions=list(permissions),
        **kw,
    )


class TestPasswordHash:
    def test_a_password_checks_against_its_hash(self):
        stored = hash_password(PASSWORD)
        assert verify_password(PASSWORD, stored)
        assert not verify_password(PASSWORD + "x", stored)
        assert not verify_password("", stored)

    def test_the_hash_is_salted_and_says_nothing_of_the_password(self):
        first, second = hash_password(PASSWORD), hash_password(PASSWORD)
        assert first != second
        assert PASSWORD not in first and first.startswith("scrypt$")

    @pytest.mark.parametrize(
        "stored",
        [
            "",
            "garbage",
            "scrypt$16$8$1$@@@$@@@",
            "scrypt$1073741824$8$1$AAAA$AAAA",  # a planted record cannot ask for gigabytes
            "scrypt$15$8$1$AAAA$AAAA",  # not a power of two
            "scrypt$16$8$99$AAAA$AAAA",
        ],
    )
    def test_a_damaged_or_planted_hash_is_never_a_match_and_never_a_crash(self, stored):
        assert verify_password(PASSWORD, stored) is False

    def test_an_absurdly_long_password_is_refused_before_hashing(self):
        assert verify_password("x" * 5000, hash_password(PASSWORD)) is False

    def test_the_shipped_cost_is_worth_having(self):
        n, r, p = SHIPPED_COST
        assert n >= 2**14 and r >= 8 and p >= 1
        assert 128 * n * r <= users_domain._SCRYPT_MEMORY  # and the check can afford it


class TestRules:
    @pytest.mark.parametrize(
        ("password", "why"),
        [
            ("short", "at least 10"),
            ("a" * 2000, "at most"),
            (" leading-space-password", "space"),
            ("trailing-space-password ", "space"),
            ("aaaaaaaaaaaa", "repeated"),
            ("ayseayseayse"[:0] + "AYSE", "at least"),
        ],
    )
    def test_a_password_that_will_not_do_says_why(self, password, why):
        with pytest.raises(UserError, match=why):
            check_password(password, "ayse", 10)

    def test_a_password_cannot_be_the_username(self):
        with pytest.raises(UserError, match="username"):
            check_password("Ayse.Yilmaz", "ayse.yilmaz", 10)

    def test_length_is_what_counts_not_which_characters(self):
        check_password("purely lower case words only", "ayse", 10)

    @pytest.mark.parametrize("name", ["", "-x", "a b", "a/b", "x" * 65, "ü"])
    def test_a_bad_name_is_refused(self, name):
        with pytest.raises(UserError):
            check_name(name)

    @pytest.mark.parametrize("name", ["me", "ME", "admin", "root", "system"])
    def test_a_reserved_name_is_refused(self, name):
        with pytest.raises(UserError, match="reserved"):
            check_name(name)

    def test_a_user_gets_no_more_than_its_maker_holds(self):
        check_user_grant(LIMITED, ["read"], [])
        with pytest.raises(UserError, match="do not hold: query"):
            check_user_grant(LIMITED, ["read", "query"], [])
        with pytest.raises(UserError, match="do not hold: all"):
            check_user_grant(LIMITED, ["all"], [])
        check_user_grant(ROOT, ["all", "read"], [])

    def test_a_user_carries_exactly_its_makers_roles(self):
        ops = Grantor(name="o", permissions=("all",), roles=("ops",), has_row_rules=False)
        check_user_grant(ops, ["read"], ["ops"])
        for wrong in ([], ["ops", "dpo"], ["dpo"]):
            with pytest.raises(UserError, match="roles"):
                check_user_grant(ops, ["read"], wrong)

    def test_someone_held_to_row_rules_makes_nobody(self):
        held = Grantor(name="h", permissions=("all",), roles=(), has_row_rules=True)
        with pytest.raises(UserError, match="row rules"):
            check_user_grant(held, ["read"], [])

    def test_unknown_and_empty_permissions_are_refused(self):
        with pytest.raises(UserError, match="Unknown"):
            check_user_grant(ROOT, ["read", "godmode"], [])
        with pytest.raises(UserError, match="at least one"):
            check_user_grant(ROOT, [], [])

    def test_nobody_manages_an_account_stronger_than_their_own(self):
        def user(perms, roles=()):
            return ManagedUser(
                name="t",
                password_hash="x",
                permissions=tuple(perms),
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                created_by=None,
                roles=tuple(roles),
            )

        check_may_manage(LIMITED, user(["read"]))
        check_may_manage(ROOT, user(["all"]))
        with pytest.raises(UserError, match="more than you"):
            check_may_manage(LIMITED, user(["all"]))
        with pytest.raises(UserError, match="more than you"):
            check_may_manage(LIMITED, user(["read", "query"]))
        with pytest.raises(UserError, match="other roles"):
            check_may_manage(ROOT, user(["read"], ["dpo"]))


class TestStore:
    def test_users_survive_a_restart_and_the_file_holds_no_password(self, tmp_path):
        svc = service(tmp_path)
        make(svc, "ayse", display="Ayşe Yılmaz")
        text = (tmp_path / "users.json").read_text(encoding="utf-8")
        assert PASSWORD not in text and "password_hash" in text
        again = service(tmp_path)
        assert [u.name for u in again.users()] == ["ayse"]
        assert again.get("AYSE").display == "Ayşe Yılmaz"

    def test_the_file_is_readable_by_its_owner_only(self, tmp_path):
        make(service(tmp_path))
        assert stat.S_IMODE(os.stat(tmp_path / "users.json").st_mode) == 0o600

    def test_a_change_in_one_worker_is_seen_by_another(self, tmp_path):
        one, other = service(tmp_path), service(tmp_path)
        make(one, "ayse")
        assert other.get("ayse") is not None
        one.update(ROOT, "ayse", disabled=True)
        assert other.get("ayse").disabled is True

    def test_two_workers_making_users_at_once_lose_neither(self, tmp_path):
        a, b = service(tmp_path), service(tmp_path)
        threads = [
            threading.Thread(target=lambda s=s, n=n: make(s, n))
            for s, n in [(a, "user-a1"), (b, "user-b1"), (a, "user-a2"), (b, "user-b2")]
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(u.name for u in service(tmp_path).users()) == [
            "user-a1",
            "user-a2",
            "user-b1",
            "user-b2",
        ]

    def test_a_damaged_file_keeps_the_users_last_read(self, tmp_path):
        svc = service(tmp_path)
        make(svc, "ayse")
        assert svc.get("ayse")
        (tmp_path / "users.json").write_text("{not json", encoding="utf-8")
        assert svc.get("ayse") is not None  # nobody is locked out by a bad write


class TestService:
    def test_a_made_user_has_what_it_was_given(self, tmp_path):
        svc = service(tmp_path)
        user = make(svc, "ayse", ("read", "query"), display=" Ayşe ", tenant="a")
        assert user.permissions == ("read", "query") and user.display == "Ayşe"
        assert user.tenant == "a" and user.created_by == "root" and user.roles == ()
        caller = svc.caller_for("ayse")
        assert caller is not None and sorted(caller.permissions) == ["query", "read"]

    def test_names_are_unique_in_any_case_and_never_a_configured_keys(self, tmp_path):
        svc = service(tmp_path)
        make(svc, "ayse")
        for taken in ("AYSE", "Ayse", "service-key", "SERVICE-KEY"):
            with pytest.raises(UserError, match="already exists"):
                make(svc, taken)

    def test_a_weak_password_makes_no_user(self, tmp_path):
        svc = service(tmp_path)
        with pytest.raises(UserError):
            make(svc, "ayse", password="short")
        assert svc.users() == []

    def test_what_a_user_may_do_changes_within_what_the_changer_holds(self, tmp_path):
        svc = service(tmp_path)
        make(svc, "ayse", ("read",))
        with pytest.raises(UserError, match="do not hold"):
            svc.update(LIMITED, "ayse", permissions=["read", "query"])
        assert svc.update(ROOT, "ayse", permissions=["read", "audit"]).permissions == (
            "read",
            "audit",
        )

    def test_display_and_tenant_change_and_clear(self, tmp_path):
        svc = service(tmp_path)
        make(svc, "ayse", tenant="a")
        user = svc.update(ROOT, "ayse", display="A", tenant="b")
        assert (user.display, user.tenant) == ("A", "b")
        assert svc.update(ROOT, "ayse", clear_tenant=True).tenant is None

    def test_nobody_changes_an_account_stronger_than_their_own(self, tmp_path):
        svc = service(tmp_path)
        make(svc, "boss", ("all",))
        for attempt in (
            lambda: svc.update(LIMITED, "boss", disabled=True),
            lambda: svc.set_password(LIMITED, "boss", "a-new-long-password"),
            lambda: svc.delete(LIMITED, "boss"),
        ):
            with pytest.raises(UserError, match="more than you"):
                attempt()
        assert svc.get("boss").disabled is False

    def test_nobody_signs_themselves_out_or_demotes_themselves(self, tmp_path):
        svc = service(tmp_path)
        make(svc, "boss", ("all",))
        me = Grantor(name="boss", permissions=("all",), roles=(), has_row_rules=False)
        for attempt in (
            lambda: svc.update(me, "boss", disabled=True),
            lambda: svc.update(me, "boss", permissions=["read"]),
            lambda: svc.delete(me, "boss"),
            lambda: svc.set_password(me, "boss", "another-long-password"),
        ):
            with pytest.raises(UserError):
                attempt()
        svc.update(me, "boss", display="The Boss")  # what cannot lock anyone out is allowed

    def test_an_unknown_user_is_not_found(self, tmp_path):
        svc = service(tmp_path)
        for attempt in (
            lambda: svc.update(ROOT, "ghost", disabled=True),
            lambda: svc.delete(ROOT, "ghost"),
            lambda: svc.set_password(ROOT, "ghost", "a-new-long-password"),
        ):
            with pytest.raises(LookupError):
                attempt()

    def test_removing_a_user_removes_them(self, tmp_path):
        svc = service(tmp_path)
        make(svc, "ayse")
        svc.delete(ROOT, "ayse")
        assert svc.get("ayse") is None and svc.caller_for("ayse") is None


class TestSigningIn:
    def test_the_right_password_signs_in_and_records_it(self, tmp_path):
        clock = Clock()
        svc = service(tmp_path, clock)
        make(svc, "ayse")
        user = svc.authenticate("AYSE", PASSWORD)
        assert user.name == "ayse"
        assert svc.get("ayse").last_sign_in == clock.at

    def test_wrong_password_unknown_user_and_disabled_account_read_the_same(self, tmp_path):
        svc = service(tmp_path)
        make(svc, "ayse")
        make(svc, "off")
        svc.update(ROOT, "off", disabled=True)
        answers = set()
        for name, password in [("ayse", "wrong"), ("nobody", PASSWORD), ("off", PASSWORD)]:
            with pytest.raises(SignInRefused) as refused:
                svc.authenticate(name, password)
            answers.add(str(refused.value))
        assert answers == {REFUSED}

    @pytest.mark.parametrize("name", ["", "a" * 100, "\x00ayse"])
    def test_an_impossible_name_is_refused_without_hashing(self, tmp_path, name, monkeypatch):
        svc = service(tmp_path)
        monkeypatch.setattr(
            "truejoin.application.local_users.verify_password",
            lambda *a: pytest.fail("should not be hashed"),
        )
        with pytest.raises(SignInRefused):
            svc.authenticate(name, PASSWORD)

    def test_a_missing_name_costs_a_hash_as_a_wrong_password_does(self, tmp_path, monkeypatch):
        svc = service(tmp_path)
        make(svc, "ayse")
        calls: list[str] = []
        real = users_domain.verify_password
        monkeypatch.setattr(
            "truejoin.application.local_users.verify_password",
            lambda password, stored: calls.append(stored) or real(password, stored),
        )
        for name in ("ayse", "nobody"):
            with pytest.raises(SignInRefused):
                svc.authenticate(name, "wrong")
        assert len(calls) == 2 and calls[1] == users_domain.DUMMY_HASH

    def test_repeated_failures_pause_that_user_only_and_a_reset_lifts_it(self, tmp_path):
        svc = service(tmp_path, max_failures=3)
        make(svc, "ayse")
        make(svc, "deniz")
        for _ in range(3):
            with pytest.raises(SignInRefused):
                svc.authenticate("ayse", "wrong")
        with pytest.raises(TooManyFailures):
            svc.authenticate("ayse", PASSWORD)
        assert svc.authenticate("deniz", PASSWORD)
        svc.set_password(ROOT, "ayse", "a-brand-new-password")
        assert svc.authenticate("ayse", "a-brand-new-password")

    def test_one_address_trying_many_names_is_paused_eventually(self, tmp_path):
        svc = service(tmp_path, max_failures=1)
        make(svc, "ayse")
        for n in range(10):
            with pytest.raises(SignInRefused):
                svc.authenticate(f"user{n}", "guess", client="10.0.0.9")
        with pytest.raises(TooManyFailures):
            svc.authenticate("ayse", PASSWORD, client="10.0.0.9")
        assert svc.authenticate("ayse", PASSWORD, client="10.0.0.10")

    def test_an_administrator_resets_a_password_and_the_old_one_stops_working(self, tmp_path):
        svc = service(tmp_path)
        make(svc, "ayse")
        svc.set_password(ROOT, "ayse", "a-brand-new-password")
        with pytest.raises(SignInRefused):
            svc.authenticate("ayse", PASSWORD)
        assert svc.authenticate("ayse", "a-brand-new-password")

    def test_a_user_changes_their_own_password_knowing_the_current_one(self, tmp_path):
        svc = service(tmp_path)
        make(svc, "ayse")
        with pytest.raises(SignInRefused, match="current"):
            svc.change_password("ayse", "wrong", "a-brand-new-password")
        with pytest.raises(UserError, match="differ"):
            svc.change_password("ayse", PASSWORD, PASSWORD)
        with pytest.raises(UserError, match="at least"):
            svc.change_password("ayse", PASSWORD, "short")
        svc.change_password("ayse", PASSWORD, "a-brand-new-password")
        assert svc.authenticate("ayse", "a-brand-new-password")

    def test_guessing_the_current_password_is_throttled_too(self, tmp_path):
        svc = service(tmp_path, max_failures=2)
        make(svc, "ayse")
        for _ in range(2):
            with pytest.raises(SignInRefused):
                svc.change_password("ayse", "wrong", "a-brand-new-password")
        with pytest.raises(TooManyFailures):
            svc.change_password("ayse", PASSWORD, "a-brand-new-password")

    def test_a_password_change_moves_the_password_version_even_to_the_same_words(self, tmp_path):
        svc = service(tmp_path)
        user = make(svc, "ayse")
        again = svc.set_password(ROOT, "ayse", PASSWORD)  # the same words, a new salted hash
        assert password_version(again) != password_version(user)
        assert password_version(svc.get("ayse")) == password_version(again)


class FakeDirectory:
    """Stands where the LDAP authenticator stands: records who it is asked about."""

    def __init__(self) -> None:
        self.asked: list[str] = []
        self.config = type("C", (), {"session_ttl_seconds": 3600})()

    def authenticate(self, username, password, client=""):
        self.asked.append(username)
        if password != "directory-password":
            raise SignInRefused(REFUSED)
        return type(
            "P",
            (),
            {"username": username, "groups": ("analysts",), "tenant": None, "display": None},
        )()

    def roles(self, groups):
        return ["analyst"]

    def caller(self, name, roles, tenant):
        return ApiKeyConfig(key="", name=name, permissions=["read"], roles=roles, tenant=tenant)


def manager(tmp_path, ldap=None, clock=None, **overrides):
    svc = service(tmp_path, clock, **overrides)
    cfg = AuthConfig(
        enabled=True,
        users=svc.config,
        api_keys=[ApiKeyConfig(key="root-key-0123456789", name="service-key", permissions=["all"])],
    )
    return (
        AuthManager(cfg, users=svc, ldap=ldap, sessions=SessionTokens(3600, key=KEY)),
        svc,
    )


class Req:
    def __init__(self, token=None) -> None:
        from starlette.requests import Request as Starlette

        headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
        self.request = Starlette(
            {
                "type": "http",
                "headers": headers,
                "path": "/x",
                "scheme": "http",
                "server": ("testserver", 80),
                "query_string": b"",
            }
        )


async def who(auth: AuthManager, token: str):
    return await auth.get_current_user(Req(token).request)


class TestAuthManager:
    async def test_the_token_a_sign_in_gives_is_a_bearer_token_with_the_users_permissions(
        self, tmp_path
    ):
        auth, svc = manager(tmp_path)
        make(svc, "ayse", ("read", "query"), display="Ayşe")
        signed = auth.password_sign_in("ayse", PASSWORD)
        assert signed["name"] == "Ayşe" and signed["access_token"].count(".") == 2
        user = await who(auth, signed["access_token"])
        assert user.name == "ayse" and user.has_permission(Permission.QUERY)
        assert not user.has_permission(Permission.UPDATE)

    async def test_disabling_a_user_ends_the_sign_in_they_already_hold(self, tmp_path):
        from fastapi import HTTPException

        auth, svc = manager(tmp_path)
        make(svc, "ayse")
        token = auth.password_sign_in("ayse", PASSWORD)["access_token"]
        svc.update(ROOT, "ayse", disabled=True)
        with pytest.raises(HTTPException) as refused:
            await who(auth, token)
        assert refused.value.status_code == 401 and "no longer active" in refused.value.detail

    async def test_removing_a_user_ends_it_too(self, tmp_path):
        from fastapi import HTTPException

        auth, svc = manager(tmp_path)
        make(svc, "ayse")
        token = auth.password_sign_in("ayse", PASSWORD)["access_token"]
        svc.delete(ROOT, "ayse")
        with pytest.raises(HTTPException):
            await who(auth, token)

    async def test_a_changed_password_ends_the_sign_ins_made_with_the_old_one(self, tmp_path):
        from fastapi import HTTPException

        auth, svc = manager(tmp_path)
        make(svc, "ayse")
        token = auth.password_sign_in("ayse", PASSWORD)["access_token"]
        svc.set_password(ROOT, "ayse", "a-brand-new-password")
        with pytest.raises(HTTPException) as refused:
            await who(auth, token)
        assert "password changed" in refused.value.detail
        fresh = auth.password_sign_in("ayse", "a-brand-new-password")["access_token"]
        assert (await who(auth, fresh)).name == "ayse"

    async def test_what_a_user_may_do_is_read_when_the_token_is_used(self, tmp_path):
        auth, svc = manager(tmp_path)
        make(svc, "ayse", ("read", "query"))
        token = auth.password_sign_in("ayse", PASSWORD)["access_token"]
        svc.update(ROOT, "ayse", permissions=["read"])
        assert not (await who(auth, token)).has_permission(Permission.QUERY)

    async def test_a_forged_token_for_a_local_user_is_refused(self, tmp_path):
        import jwt
        from fastapi import HTTPException

        auth, svc = manager(tmp_path)
        make(svc, "ayse", ("all",))
        forged = jwt.encode(
            {
                "iss": "truejoin",
                "sub": "ayse",
                "iat": 1,
                "exp": 9999999999,
                "src": "local",
                "pv": password_version(svc.get("ayse")),
            },
            b"x" * 32,
            algorithm="HS256",
        )
        with pytest.raises(HTTPException) as refused:
            await who(auth, forged)
        assert refused.value.status_code == 401

    async def test_a_local_name_never_reaches_the_directory(self, tmp_path):
        directory = FakeDirectory()
        auth, svc = manager(tmp_path, ldap=directory)
        make(svc, "ayse")
        with pytest.raises(SignInRefused):
            auth.password_sign_in("ayse", "directory-password")  # the directory's would work...
        assert directory.asked == []  # ...but a local name is only ever checked locally
        assert auth.password_sign_in("ayse", PASSWORD)["access_token"]

    async def test_any_other_name_goes_to_the_directory(self, tmp_path):
        directory = FakeDirectory()
        auth, svc = manager(tmp_path, ldap=directory)
        signed = auth.password_sign_in("ali", "directory-password")
        assert directory.asked == ["ali"]
        assert (await who(auth, signed["access_token"])).name == "ali"

    async def test_with_only_local_users_an_unknown_name_is_refused_alike(self, tmp_path):
        auth, svc = manager(tmp_path)
        make(svc, "ayse")
        for name in ("nobody", "ayse"):
            with pytest.raises(SignInRefused, match=REFUSED):
                auth.password_sign_in(name, "wrong-wrong-wrong")

    def test_password_sign_in_is_offered_with_local_users_alone(self, tmp_path):
        auth, _ = manager(tmp_path)
        assert auth.password_sign_in_available


class Sink:
    def __init__(self) -> None:
        self.events: list = []

    def record(self, event) -> None:
        self.events.append(event)


def app_with(tmp_path, *, store=True, enabled=True):
    svc = service(tmp_path) if store else None
    cfg = AuthConfig(
        enabled=enabled,
        users=svc.config if svc else None,
        api_keys=[
            ApiKeyConfig(key="root-key", name="service-key", permissions=["all"]),
            ApiKeyConfig(key="admin-key", name="helper", permissions=["read", "admin"]),
            ApiKeyConfig(key="reader-key", name="reader", permissions=["read"]),
        ],
    )
    auth = AuthManager(cfg, users=svc, sessions=SessionTokens(3600, key=KEY))
    sink = Sink()
    app = FastAPI()
    app.include_router(create_users_router(svc, auth, sink), prefix="/api/v1")

    @app.get("/data", dependencies=[Depends(auth.require(Permission.READ))])
    async def data(request: Request) -> dict:
        caller = caller_of(request)
        return {"name": caller.name if caller else None}

    return TestClient(app), auth, svc, sink


def as_(key: str) -> dict:
    return {"X-API-Key": key}


def bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


NEW = {"name": "ayse", "password": PASSWORD, "permissions": ["read", "query"], "display": "Ayşe"}


class TestRoutes:
    def test_an_administrator_makes_lists_and_the_hash_never_leaves(self, tmp_path):
        client, _, _, _ = app_with(tmp_path)
        made = client.post("/api/v1/auth/users", json=NEW, headers=as_("root-key"))
        assert made.status_code == 201 and made.headers["cache-control"] == "no-store"
        listed = client.get("/api/v1/auth/users", headers=as_("root-key")).json()
        assert [u["name"] for u in listed["users"]] == ["ayse"]
        assert listed["available"] is True and "scrypt" not in json.dumps(listed)
        assert PASSWORD not in json.dumps(listed) and PASSWORD not in made.text
        assert "password_hash" not in made.text and "password_hash" not in json.dumps(listed)

    def test_only_an_administrator_manages_users(self, tmp_path):
        client, _, _, _ = app_with(tmp_path)
        for call in (
            lambda h: client.get("/api/v1/auth/users", headers=h),
            lambda h: client.post("/api/v1/auth/users", json=NEW, headers=h),
            lambda h: client.patch("/api/v1/auth/users/x", json={"disabled": True}, headers=h),
            lambda h: client.delete("/api/v1/auth/users/x", headers=h),
            lambda h: client.post(
                "/api/v1/auth/users/x/password", json={"password": PASSWORD}, headers=h
            ),
        ):
            assert call(as_("reader-key")).status_code == 403
            assert call({}).status_code == 401

    def test_a_weak_password_a_duplicate_and_a_reserved_name_are_422(self, tmp_path):
        client, _, _, _ = app_with(tmp_path)
        h = as_("root-key")
        assert (
            client.post(
                "/api/v1/auth/users", json={**NEW, "password": "short"}, headers=h
            ).status_code
            == 422
        )
        assert client.post("/api/v1/auth/users", json=NEW, headers=h).status_code == 201
        assert (
            client.post("/api/v1/auth/users", json={**NEW, "name": "AYSE"}, headers=h).status_code
            == 422
        )
        assert (
            client.post("/api/v1/auth/users", json={**NEW, "name": "me"}, headers=h).status_code
            == 422
        )
        assert (
            client.post(
                "/api/v1/auth/users", json={**NEW, "name": "service-key"}, headers=h
            ).status_code
            == 422
        )

    def test_a_limited_administrator_cannot_escalate_or_reach_a_stronger_account(self, tmp_path):
        client, _, _, _ = app_with(tmp_path)
        client.post(
            "/api/v1/auth/users",
            json={**NEW, "name": "boss", "permissions": ["all"]},
            headers=as_("root-key"),
        )
        h = as_("admin-key")
        assert (
            client.post(
                "/api/v1/auth/users", json={**NEW, "name": "x", "permissions": ["all"]}, headers=h
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/v1/auth/users", json={**NEW, "name": "x", "permissions": ["query"]}, headers=h
            ).status_code
            == 422
        )
        assert (
            client.post(
                "/api/v1/auth/users", json={**NEW, "name": "x", "permissions": ["read"]}, headers=h
            ).status_code
            == 201
        )
        for attempt in (
            lambda: client.post(
                "/api/v1/auth/users/boss/password",
                json={"password": "hijacked-password"},
                headers=h,
            ),
            lambda: client.patch("/api/v1/auth/users/boss", json={"disabled": True}, headers=h),
            lambda: client.delete("/api/v1/auth/users/boss", headers=h),
        ):
            assert attempt().status_code == 422

    def test_change_disable_reset_and_remove(self, tmp_path):
        client, _, svc, _ = app_with(tmp_path)
        h = as_("root-key")
        client.post("/api/v1/auth/users", json=NEW, headers=h)
        patched = client.patch(
            "/api/v1/auth/users/ayse", json={"permissions": ["read"], "disabled": True}, headers=h
        )
        assert patched.status_code == 200 and patched.json()["user"]["disabled"] is True
        assert patched.json()["user"]["permissions"] == ["read"]
        assert (
            client.post(
                "/api/v1/auth/users/ayse/password",
                json={"password": "a-brand-new-password"},
                headers=h,
            ).status_code
            == 200
        )
        assert svc.get("ayse").permissions == ("read",)
        assert client.delete("/api/v1/auth/users/ayse", headers=h).status_code == 200
        assert client.delete("/api/v1/auth/users/ayse", headers=h).status_code == 404
        assert (
            client.patch("/api/v1/auth/users/ayse", json={"disabled": False}, headers=h).status_code
            == 404
        )

    def test_a_user_changes_their_own_password_and_the_old_sign_in_ends(self, tmp_path):
        client, auth, svc, _ = app_with(tmp_path)
        client.post("/api/v1/auth/users", json=NEW, headers=as_("root-key"))
        token = auth.password_sign_in("ayse", PASSWORD)["access_token"]
        body = {"current_password": PASSWORD, "new_password": "a-brand-new-password"}
        assert client.put("/api/v1/auth/users/me/password", json=body).status_code == 401
        wrong = client.put(
            "/api/v1/auth/users/me/password",
            json={**body, "current_password": "nope-nope-nope"},
            headers=bearer(token),
        )
        assert wrong.status_code == 403
        done = client.put("/api/v1/auth/users/me/password", json=body, headers=bearer(token))
        assert done.status_code == 200 and done.json()["changed"] is True
        assert client.get("/data", headers=bearer(token)).status_code == 401
        fresh = auth.password_sign_in("ayse", "a-brand-new-password")["access_token"]
        assert client.get("/data", headers=bearer(fresh)).json() == {"name": "ayse"}

    def test_me_is_a_route_not_a_user_called_me(self, tmp_path):
        client, _, _, _ = app_with(tmp_path)
        r = client.put(
            "/api/v1/auth/users/me/password",
            json={"current_password": "a", "new_password": "b"},
            headers=as_("root-key"),
        )
        assert r.status_code in (403, 404, 422)  # a key has no password: never "No user me" success

    def test_without_a_store_users_cannot_be_made(self, tmp_path):
        client, _, _, _ = app_with(tmp_path, store=False)
        listed = client.get("/api/v1/auth/users", headers=as_("root-key")).json()
        assert listed["available"] is False and "auth.users.store" in listed["detail"]
        assert (
            client.post("/api/v1/auth/users", json=NEW, headers=as_("root-key")).status_code == 501
        )

    def test_with_authentication_off_a_user_would_not_be_checked_so_none_is_made(self, tmp_path):
        client, _, _, _ = app_with(tmp_path, enabled=False)
        assert client.post("/api/v1/auth/users", json=NEW).status_code == 409

    def test_every_change_is_audited_by_name_and_never_with_a_password(self, tmp_path):
        client, _, _, sink = app_with(tmp_path)
        h = as_("root-key")
        client.post("/api/v1/auth/users", json=NEW, headers=h)
        client.patch("/api/v1/auth/users/ayse", json={"disabled": True}, headers=h)
        client.post(
            "/api/v1/auth/users/ayse/password", json={"password": "a-brand-new-password"}, headers=h
        )
        client.delete("/api/v1/auth/users/ayse", headers=h)
        changes = [e.change for e in sink.events]
        assert changes == [
            "create_user ayse",
            "update_user ayse disabled",
            "reset_password ayse",
            "delete_user ayse",
        ]
        assert all(e.actor == "service-key" for e in sink.events)
        assert "password" not in json.dumps([e.change for e in sink.events]).replace(
            "reset_password", ""
        )


class TestConsole:
    def client(self, tmp_path, **overrides):
        from truejoin.adapters.inbound.http.app import create_app, runtime_of
        from truejoin.application.settings import RuntimeEnv

        auth, svc = manager(tmp_path, **overrides)
        app = create_app(env=RuntimeEnv())
        runtime_of(app).auth_manager = auth
        return TestClient(app), svc

    def test_the_console_is_told_a_password_sign_in_exists(self, tmp_path):
        client, _ = self.client(tmp_path)
        assert client.get("/ui/sign-in").json() == {"enabled": False, "password": True}

    def test_a_local_user_signs_in_from_the_console(self, tmp_path):
        client, svc = self.client(tmp_path)
        make(svc, "ayse", ("read", "query"), display="Ayşe")
        r = client.post("/ui/sign-in/password", json={"username": "ayse", "password": PASSWORD})
        assert r.status_code == 200 and r.json()["name"] == "Ayşe"
        access = client.get("/ui/access", headers=bearer(r.json()["access_token"])).json()
        assert access["actor"] == "ayse" and access["query"] is True and access["admin"] is False

    def test_wrong_passwords_are_401_and_repeated_ones_429(self, tmp_path):
        client, svc = self.client(tmp_path, max_failures=2)
        make(svc, "ayse")
        wrong = {"username": "ayse", "password": "nope-nope-nope"}
        assert client.post("/ui/sign-in/password", json=wrong).json()["detail"] == REFUSED
        client.post("/ui/sign-in/password", json=wrong)
        paused = client.post(
            "/ui/sign-in/password", json={"username": "ayse", "password": PASSWORD}
        )
        assert paused.status_code == 429 and int(paused.headers["Retry-After"]) > 0


class TestCli:
    def config_file(self, tmp_path):
        path = tmp_path / "database.yaml"
        path.write_text(
            "databases: []\n"
            "settings:\n"
            "  auth:\n"
            "    enabled: true\n"
            "    api_keys:\n"
            "      - {key: service-key-0123456789, name: svc, permissions: [read]}\n"
            f"    users: {{store: {tmp_path / 'users.json'}, min_password_length: 10}}\n",
            encoding="utf-8",
        )
        return str(path)

    def run(self, tmp_path, *args):
        return CliRunner().invoke(users_cli, [*args, "-c", self.config_file(tmp_path)])

    def test_the_first_administrator_is_made_and_can_sign_in(self, tmp_path):
        made = self.run(tmp_path, "add", "yonetici", "--permission", "all", "--password", PASSWORD)
        assert made.exit_code == 0, made.output
        listed = self.run(tmp_path, "list")
        assert "yonetici" in listed.output and "all" in listed.output
        svc = service(tmp_path)
        assert svc.authenticate("yonetici", PASSWORD).permissions == ("all",)
        assert PASSWORD not in (tmp_path / "users.json").read_text()

    def test_a_weak_password_a_taken_name_and_a_key_name_are_refused(self, tmp_path):
        assert self.run(tmp_path, "add", "ayse", "--password", "short").exit_code == 1
        self.run(tmp_path, "add", "ayse", "--password", PASSWORD)
        assert self.run(tmp_path, "add", "ayse", "--password", PASSWORD).exit_code == 1
        assert self.run(tmp_path, "add", "svc", "--password", PASSWORD).exit_code == 1

    def test_passwd_disable_and_enable(self, tmp_path):
        self.run(tmp_path, "add", "ayse", "--password", PASSWORD)
        assert (
            self.run(tmp_path, "passwd", "ayse", "--password", "a-brand-new-password").exit_code
            == 0
        )
        assert self.run(tmp_path, "disable", "ayse").exit_code == 0
        svc = service(tmp_path)
        with pytest.raises(SignInRefused):
            svc.authenticate("ayse", "a-brand-new-password")
        assert self.run(tmp_path, "enable", "ayse").exit_code == 0
        assert service(tmp_path).authenticate("ayse", "a-brand-new-password")

    def test_passwd_reaches_an_account_with_roles_the_cli_does_not_have(self, tmp_path):
        svc = service(tmp_path)
        ops = Grantor(name="o", permissions=("all",), roles=("ops",), has_row_rules=False)
        svc.create(ops, name="opsy", password=PASSWORD, permissions=["read"], roles=["ops"])
        assert (
            self.run(tmp_path, "passwd", "opsy", "--password", "a-brand-new-password").exit_code
            == 0
        )

    def test_an_unknown_user_and_a_config_without_users_are_clear_errors(self, tmp_path):
        assert (
            "No user ghost" in self.run(tmp_path, "passwd", "ghost", "--password", PASSWORD).output
        )
        bare = tmp_path / "bare.yaml"
        bare.write_text("databases: []\n", encoding="utf-8")
        out = CliRunner().invoke(users_cli, ["list", "-c", str(bare)])
        assert out.exit_code == 1 and "auth.users" in out.output


class TestConfiguration:
    def production(self, monkeypatch, secret=True, enabled=True):
        if secret:
            monkeypatch.setenv("TRUEJOIN_SECRET_KEY", "x" * 44)
        else:
            monkeypatch.delenv("TRUEJOIN_SECRET_KEY", raising=False)
        cfg = Settings(
            databases=[],
            settings={"auth": {"enabled": enabled, "users": {"store": "users.json"}}},
        )
        return validate_production_config(cfg, "production")

    def test_production_needs_a_secret_for_sessions(self, monkeypatch):
        assert any("TRUEJOIN_SECRET_KEY" in p for p in self.production(monkeypatch, secret=False))
        assert not [p for p in self.production(monkeypatch) if "auth.users" in p]

    def test_users_with_authentication_off_are_refused(self, monkeypatch):
        assert any(
            "auth.users" in p and "enabled" in p
            for p in self.production(monkeypatch, enabled=False)
        )

    @pytest.mark.parametrize(
        "bad", [{"min_password_length": 4}, {"session_ttl_seconds": 5}, {"store": ""}]
    )
    def test_silly_values_are_refused(self, bad):
        with pytest.raises(ValueError):
            LocalUsersConfig(**{"store": "u.json", **bad})


class TestSignInEvents:
    """Every password sign-in, and every key or token refused, is one event: never the secret."""

    class Events:
        def __init__(self) -> None:
            self.got = []

        def record(self, event) -> None:
            self.got.append(event)

    def manager(self, tmp_path, **overrides):
        auth, svc = manager(tmp_path, **overrides)
        auth.audit = events = self.Events()
        return auth, svc, events

    def test_a_password_sign_in_and_a_refusal(self, tmp_path):
        auth, svc, events = self.manager(tmp_path)
        make(svc, "ayse")
        auth.password_sign_in("ayse", PASSWORD, client="10.0.0.5")
        with pytest.raises(SignInRefused):
            auth.password_sign_in("ayse", "wrong-wrong-wrong", client="10.0.0.6")
        got = [(e.method, e.outcome, e.actor, e.client) for e in events.got]
        assert got == [
            ("local", "signed_in", "ayse", "10.0.0.5"),
            ("local", "refused", "ayse", "10.0.0.6"),
        ]
        assert all(PASSWORD not in str(e.as_dict()) for e in events.got)
        assert all("wrong-wrong" not in str(e.as_dict()) for e in events.got)

    def test_a_paused_name_is_recorded_as_paused(self, tmp_path):
        auth, svc, events = self.manager(tmp_path, max_failures=1)
        make(svc, "ayse")
        for _ in range(3):
            with pytest.raises((SignInRefused, TooManyFailures)):
                auth.password_sign_in("ayse", "wrong-wrong-wrong")
        assert "paused" in [e.outcome for e in events.got]

    async def test_a_key_or_a_token_that_is_refused_is_recorded_without_it(self, tmp_path):
        from fastapi import HTTPException
        from starlette.requests import Request as Starlette

        auth, _, events = self.manager(tmp_path)
        scope = {
            "type": "http",
            "path": "/x",
            "scheme": "http",
            "server": ("testserver", 80),
            "client": ("10.1.1.1", 5555),
            "query_string": b"",
            "headers": [(b"x-api-key", b"not-a-real-key-123")],
        }
        with pytest.raises(HTTPException):
            await auth.get_current_user(Starlette(scope))
        with pytest.raises(HTTPException):
            await who(auth, "not.a.token")
        key, token = events.got
        assert (key.method, key.outcome, key.client) == ("key", "refused", "10.1.1.1")
        assert token.outcome == "refused" and token.method in {"token", "provider"}
        assert "not-a-real-key" not in str(key.as_dict()) and "not.a.token" not in str(
            token.as_dict()
        )
