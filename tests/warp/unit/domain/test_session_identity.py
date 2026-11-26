"""What a database may be told about the caller, and when nothing is told."""

import asyncio
import threading

import pytest

from truejoin.adapters.outbound.warp_inproc.loop import GatewayLoop
from truejoin.application.settings import DatabaseConfig
from truejoin.domain.governance.session import (
    CURRENT_IDENTITY,
    SessionIdentity,
    SessionIdentityConfig,
    as_service,
    current_identity,
    parse_session_identity,
)

ALICE = SessionIdentity(actor="alice", tenant="a", roles=("analyst", "dpo"))


class TestParse:
    def test_nothing_asked_is_none(self):
        assert parse_session_identity("mssql", {}) is None

    def test_sql_server_keys(self):
        options = {"session_identity": {"session_context": {"actor": "who", "tenant": "tenant_id"}}}
        assert parse_session_identity("mssql", options) == SessionIdentityConfig(
            session_context=(("actor", "who"), ("tenant", "tenant_id"))
        )

    def test_oracle_identifier_and_procedure(self):
        options = {
            "session_identity": {
                "client_identifier": "actor",
                "context_procedure": "app.ctx.set_caller",
            }
        }
        parsed = parse_session_identity("oracle", options)
        assert parsed == SessionIdentityConfig(
            client_identifier="actor", context_procedure="app.ctx.set_caller"
        )

    @pytest.mark.parametrize(
        ("db_type", "setting", "message"),
        [
            ("postgresql", {"session_context": {"actor": "who"}}, "SQL Server's"),
            ("oracle", {"session_context": {"actor": "who"}}, "SQL Server's"),
            ("mssql", {"client_identifier": "actor"}, "Oracle's"),
            ("mysql", {"context_procedure": "p"}, "Oracle's"),
            ("mssql", {"session_context": {"email": "who"}}, "not one of"),
            ("mssql", {"session_context": {"actor": "who; DROP"}}, "plain key"),
            ("oracle", {"context_procedure": "p; DROP TABLE x"}, "procedure name"),
            ("oracle", {"context_procedure": "a.b.c.d"}, "procedure name"),
            ("oracle", {"client_identifier": "password"}, "not one of"),
            ("oracle", {"identifier": "actor"}, "unknown setting"),
            ("oracle", {}, "says nothing"),
            ("oracle", "actor", "mapping"),
        ],
    )
    def test_what_the_engine_cannot_use_is_refused(self, db_type, setting, message):
        with pytest.raises(ValueError, match=message):
            parse_session_identity(db_type, {"session_identity": setting})

    def test_a_database_config_is_refused_at_load(self):
        with pytest.raises(ValueError, match="SQL Server's"):
            DatabaseConfig(
                name="shop",
                type="postgresql",
                database="shop",
                username="u",
                options={"session_identity": {"session_context": {"actor": "who"}}},
            )


class TestIdentity:
    def test_fields_as_the_database_receives_them(self):
        assert (ALICE.value("actor"), ALICE.value("tenant"), ALICE.value("roles")) == (
            "alice",
            "a",
            "analyst,dpo",
        )
        assert SessionIdentity("svc").value("roles") is None

    def test_as_service_clears_and_restores(self):
        token = CURRENT_IDENTITY.set(ALICE)
        try:
            with as_service():
                assert current_identity() is None
            assert current_identity() is ALICE
        finally:
            CURRENT_IDENTITY.reset(token)

    def test_the_olap_loop_never_runs_as_the_submitting_caller(self):
        loop = GatewayLoop().start()
        seen: list[SessionIdentity | None] = []

        async def read() -> None:
            seen.append(current_identity())

        def request_thread() -> None:
            CURRENT_IDENTITY.set(ALICE)  # a signed-in request's worker thread
            loop.submit(read())
            seen.append(current_identity())  # the caller's own context is untouched

        try:
            thread = threading.Thread(target=request_thread)
            thread.start()
            thread.join()
        finally:
            loop.stop()
        assert seen == [None, ALICE]
        asyncio.set_event_loop(None)
