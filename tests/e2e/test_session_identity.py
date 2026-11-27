"""The caller reaches the database on the REST paths, and never on the OLAP side.

One process, real auth and lifespan, a fake gateway that notes who each read
ran for: the key's name for a table route, nobody for a slice the OLAP side
loads, which the next caller may be served. (That the OLAP loop drops a caller
the submitting thread does carry is held by
``tests/warp/unit/domain/test_session_identity.py``.)
"""

import pytest

from tests.e2e import test_one_process as one
from tests.fusion.fakes import FakeGateway
from truejoin.domain.governance.session import SessionIdentity, current_identity


class NotingGateway(FakeGateway):
    seen: list[tuple[FakeGateway, SessionIdentity | None]] = []

    async def select(self, *args, **kwargs):
        NotingGateway.seen.append((self, current_identity()))
        return await super().select(*args, **kwargs)

    async def stream_select(self, *args, **kwargs):
        NotingGateway.seen.append((self, current_identity()))
        async for batch in super().stream_select(*args, **kwargs):
            yield batch


@pytest.fixture
def running(tmp_path, monkeypatch):
    monkeypatch.setattr(one, "FakeGateway", NotingGateway)
    NotingGateway.seen = []
    return one.Running(tmp_path, settings={"auth": {"enabled": True, "api_keys": one.KEYS}})


def reads_of(gateway: FakeGateway) -> list[SessionIdentity | None]:
    return [who for seen, who in NotingGateway.seen if seen is gateway]


def test_a_table_route_runs_as_its_key(running):
    with running as client:
        response = client.get("/api/v1/shop/users", headers={"X-API-Key": "analyst-key"})
        assert response.status_code == 200
    routes_gateway = running.factory.created[0]
    assert reads_of(routes_gateway) == [SessionIdentity("analyst", tenant=None, roles=())]


def test_the_olap_side_loads_as_the_service_whoever_asks(running):
    sql = {"sql": "SELECT COUNT(*) AS n FROM shop.users"}
    with running as client:
        response = client.post("/olap/query", json=sql, headers={"X-API-Key": "analyst-key"})
        assert response.status_code == 200, response.text
    olap_gateway = running.factory.created[1]
    assert reads_of(olap_gateway) and set(reads_of(olap_gateway)) == {None}
