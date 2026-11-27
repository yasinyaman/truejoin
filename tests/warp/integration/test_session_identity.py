"""The database's own row security sees the caller: SQL Server policies, Oracle VPD.

A policy written by a DBA, not by Truejoin, filters a table by the identity the
session is told (``options.session_identity``): a caller sees their tenant's
rows, the service (no caller) sees them all, and a pooled session never keeps
the previous caller's identity.
"""

from collections.abc import AsyncIterator

import pytest

from truejoin.adapters.outbound.db.factory import DatabaseFactory
from truejoin.application.settings import DatabaseConfig
from truejoin.domain.governance.session import CURRENT_IDENTITY, SessionIdentity

pytestmark = pytest.mark.integration

TENANT_A = SessionIdentity("alice", tenant="a")


async def _gateway(config: DatabaseConfig, identity: dict) -> AsyncIterator:
    options = {**config.options, "session_identity": identity}
    gateway = DatabaseFactory.create(config.model_copy(update={"options": options}))
    await gateway.connect()
    try:
        yield gateway
    finally:
        await gateway.disconnect()


async def _tenants_seen(gateway, sql: str, identity: SessionIdentity | None) -> list[str]:
    token = CURRENT_IDENTITY.set(identity)
    try:
        rows = await gateway.execute_query(sql)
    finally:
        CURRENT_IDENTITY.reset(token)
    return sorted(str(next(iter(r.values()))) for r in rows)


@pytest.fixture
async def mssql_policy(mssql_config: DatabaseConfig):
    identity = {"session_context": {"tenant": "truejoin_tenant"}}
    async for gateway in _gateway(mssql_config, identity):
        statements = [
            "IF OBJECT_ID('sec.tenants') IS NOT NULL DROP SECURITY POLICY sec.tenants",
            "IF OBJECT_ID('sec.tenant_rows') IS NOT NULL DROP FUNCTION sec.tenant_rows",
            "IF OBJECT_ID('dbo.vpd_rows') IS NOT NULL DROP TABLE dbo.vpd_rows",
            "IF SCHEMA_ID('sec') IS NULL EXEC('CREATE SCHEMA sec')",
            "CREATE TABLE dbo.vpd_rows (id int PRIMARY KEY, tenant nvarchar(8))",
            "INSERT INTO dbo.vpd_rows VALUES (1, 'a'), (2, 'b'), (3, 'a')",
            "CREATE FUNCTION sec.tenant_rows(@tenant nvarchar(8)) RETURNS TABLE "
            "WITH SCHEMABINDING AS RETURN SELECT 1 AS ok "
            "WHERE SESSION_CONTEXT(N'truejoin_tenant') IS NULL "
            "OR @tenant = CAST(SESSION_CONTEXT(N'truejoin_tenant') AS nvarchar(8))",
            "CREATE SECURITY POLICY sec.tenants "
            "ADD FILTER PREDICATE sec.tenant_rows(tenant) ON dbo.vpd_rows WITH (STATE = ON)",
        ]
        for statement in statements:
            await gateway.execute_query(statement)
        try:
            yield gateway
        finally:
            for statement in statements[:3]:
                await gateway.execute_query(statement)


async def test_a_sql_server_policy_filters_by_the_caller(mssql_policy) -> None:
    sql = "SELECT tenant FROM dbo.vpd_rows"
    assert await _tenants_seen(mssql_policy, sql, TENANT_A) == ["a", "a"]
    assert await _tenants_seen(mssql_policy, sql, SessionIdentity("bob", tenant="b")) == ["b"]
    # The service, after a caller, on the same pool: every row, nothing left over.
    assert await _tenants_seen(mssql_policy, sql, None) == ["a", "a", "b"]


@pytest.fixture
async def oracle_vpd(oracle_config: DatabaseConfig):
    identity = {"client_identifier": "tenant"}
    async for gateway in _gateway(oracle_config, identity):
        await gateway.execute_query(
            "BEGIN EXECUTE IMMEDIATE 'DROP TABLE vpd_rows'; "
            "EXCEPTION WHEN OTHERS THEN IF SQLCODE != -942 THEN RAISE; END IF; END;"
        )
        await gateway.execute_query(
            "CREATE TABLE vpd_rows (id NUMBER(9) PRIMARY KEY, tenant VARCHAR2(8))"
        )
        for row in ("1, 'a'", "2, 'b'", "3, 'a'"):
            await gateway.execute_query(f"INSERT INTO vpd_rows VALUES ({row})")
        await gateway.execute_query(
            "CREATE OR REPLACE FUNCTION vpd_tenant_rows(s VARCHAR2, t VARCHAR2) RETURN VARCHAR2 AS "
            "BEGIN IF SYS_CONTEXT('USERENV', 'CLIENT_IDENTIFIER') IS NULL THEN RETURN NULL; END IF; "
            "RETURN 'tenant = SYS_CONTEXT(''USERENV'', ''CLIENT_IDENTIFIER'')'; END;"
        )
        try:
            await gateway.execute_query(
                "BEGIN DBMS_RLS.ADD_POLICY(USER, 'VPD_ROWS', 'TENANTS', USER, 'VPD_TENANT_ROWS'); END;"
            )
        except Exception as e:
            if "DBMS_RLS" in str(e) or "PLS-00201" in str(e):
                pytest.skip(f"the user may not run DBMS_RLS (docker/it/oracle-init grants it): {e}")
            raise
        try:
            yield gateway
        finally:
            await gateway.execute_query(
                "BEGIN DBMS_RLS.DROP_POLICY(USER, 'VPD_ROWS', 'TENANTS'); END;"
            )
            await gateway.execute_query("DROP TABLE vpd_rows")
            await gateway.execute_query("DROP FUNCTION vpd_tenant_rows")


async def test_an_oracle_vpd_policy_filters_by_the_client_identifier(oracle_vpd) -> None:
    sql = "SELECT tenant FROM vpd_rows"
    assert await _tenants_seen(oracle_vpd, sql, TENANT_A) == ["a", "a"]
    assert await _tenants_seen(oracle_vpd, sql, None) == ["a", "a", "b"]
