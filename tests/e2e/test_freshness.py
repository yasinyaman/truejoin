"""How fresh the analytical side's data is in a running server.

A table, once loaded, answers from what was loaded. The lifespan schedules a
refresh every ``olap.refresh_interval`` seconds, owned by the generation that
started it; an administrator can read one table again at once.
"""

import threading
import time

import pytest

from tests.e2e.test_live_settings import Server, _change
from tests.e2e.test_one_process import KEYS, Running, _settings
from truejoin.adapters.inbound.http.app import runtime_of
from truejoin.application.settings import OlapConfig

COUNT = "SELECT COUNT(*) AS n FROM shop.orders"
NEW_ORDER = {"id": 4, "user_id": 3, "amount": 1.0}


def _count(client, headers=None) -> int:
    response = client.post("/olap/query", json={"sql": COUNT}, headers=headers or {})
    assert response.status_code == 200, response.text
    return response.json()["rows"][0]["n"]


def _olap_gateway(running):
    return running.factory.created[-1]  # the routes' first, then the OLAP side's


def _jobs(fusion) -> dict[str, float]:
    return {job.name: job._interval for job in fusion.scheduler.active_jobs}


class TestTimedRefresh:
    def test_it_is_scheduled_by_default_and_stops_with_the_server(self, tmp_path):
        running = Running(tmp_path)
        with running:
            fusion = runtime_of(running.app).fusion
            assert fusion.sources.auto_refresh_running
            assert _jobs(fusion)["auto-refresh"] == 300
        assert not fusion.sources.auto_refresh_running
        assert "auto-refresh" not in _jobs(fusion)

    def test_zero_schedules_nothing(self, tmp_path):
        running = Running(tmp_path, olap={"refresh_interval": 0})
        with running:
            fusion = runtime_of(running.app).fusion
            assert not fusion.sources.auto_refresh_running

    def test_a_loaded_table_is_brought_up_to_date(self, tmp_path):
        """What the timer runs, run from another thread than the gateway loop's."""
        running = Running(tmp_path, olap={"refresh_interval": 60})
        with running as client:
            assert _count(client) == 3
            _olap_gateway(running)._tables["orders"].append(NEW_ORDER)
            assert _count(client) == 3  # held as it was loaded
            fusion = runtime_of(running.app).fusion
            timer = threading.Thread(target=fusion.sources.refresh_all, name="timer-like")
            timer.start()
            timer.join(timeout=30)
            assert _count(client) == 4

    def test_a_settings_change_moves_the_job_to_the_new_generation(self, tmp_path, monkeypatch):
        with Server(tmp_path, monkeypatch).client() as client:
            old = runtime_of(client.app).fusion
            changed = _change(client, {"olap.refresh_interval": 120})
            assert changed.status_code == 200, changed.text
            new = runtime_of(client.app).fusion
            assert new is not old
            assert _jobs(new)["auto-refresh"] == 120
            deadline = time.monotonic() + 10
            while old.sources.auto_refresh_running and time.monotonic() < deadline:
                time.sleep(0.05)  # the old generation is retired after the swap
            assert not old.sources.auto_refresh_running


class TestReloadOneTable:
    @pytest.fixture
    def running(self, tmp_path):
        return Running(tmp_path, settings={"auth": {"enabled": True, "api_keys": KEYS}})

    def test_an_administrator_reads_a_table_again(self, running):
        dpo = {"X-API-Key": "dpo-key"}  # every permission, no row rules, nothing masked
        with running as client:
            assert _count(client, dpo) == 3
            _olap_gateway(running)._tables["orders"].append(NEW_ORDER)
            assert _count(client, dpo) == 3
            response = client.post("/olap/tables/shop.orders/refresh", headers=dpo)
            assert response.status_code == 200, response.text
            assert response.json() == {
                "status": "refreshed",
                "table": "shop.orders",
                "how": "reloaded",
                "row_count": 4,
                "dropped_slices": 0,
            }
            assert _count(client, dpo) == 4

    @pytest.mark.parametrize(
        ("key", "status"),
        [
            ("analyst-key", 403),  # may query, is no administrator
            ("tenant-key", 403),  # an administrator held to row rules
            ("reader-key", 403),  # may not use /olap at all
            (None, 401),
        ],
    )
    def test_anyone_else_is_refused(self, running, key, status):
        headers = {"X-API-Key": key} if key else {}
        with running as client:
            response = client.post("/olap/tables/shop.orders/refresh", headers=headers)
            assert response.status_code == status

    def test_an_unknown_table_is_named(self, running):
        with running as client:
            response = client.post(
                "/olap/tables/shop.ordrs/refresh", headers={"X-API-Key": "dpo-key"}
            )
            assert response.status_code == 400
            assert "ordrs" in response.text

    def test_without_auth_anyone_is_an_administrator(self, tmp_path):
        with Running(tmp_path) as client:
            assert client.post("/olap/tables/shop.users/refresh").status_code == 200


class TestTheSetting:
    @pytest.mark.parametrize("value", [0, 30, 300, 86400])
    def test_never_or_at_least_thirty_seconds(self, value):
        assert OlapConfig(refresh_interval=value).to_fusion_settings().refresh_interval == value

    @pytest.mark.parametrize("value", [1, 29, -5])
    def test_a_few_seconds_is_refused(self, value):
        with pytest.raises(ValueError, match="refresh_interval"):
            OlapConfig(refresh_interval=value)

    def test_it_defaults_to_five_minutes(self, tmp_path):
        assert _settings(tmp_path).olap.refresh_interval == 300
