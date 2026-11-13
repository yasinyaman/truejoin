"""Keeping the list of added databases and its rules."""

from contextlib import nullcontext

import pytest

from truejoin.application.databases import DatabaseService
from truejoin.domain.gateway.managed import ManagedDatabaseError


class MemoryStore:
    def __init__(self):
        self.saved = []

    def version(self):
        return len(self.saved)

    def load(self):
        return list(self.saved)

    def save(self, databases):
        self.saved = list(databases)

    def locked(self):
        return nullcontext()


NEW = {"name": "sales", "type": "postgresql", "database": "sales", "username": "app"}


@pytest.fixture
def service():
    return DatabaseService(MemoryStore(), configured=["shop"], supported_types=["postgresql"])


def test_add_change_remove(service):
    database = service.prepare({**NEW, "password": "p"}, by="admin")
    assert (database.port, database.created_by) == (5432, "admin")
    service.add(database)
    changed = service.change("sales", {"host": "db2", "password": ""}, by="ops")
    service.replace(changed)
    kept = service.get("sales")
    assert (kept.host, kept.password, kept.updated_by) == ("db2", "p", "ops")
    assert service.remove("sales").name == "sales"
    assert service.all() == []


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ({**NEW, "name": "shop"}, "already exists"),
        ({**NEW, "type": "mongodb"}, "not a supported type"),
        ({**NEW, "port": 70000}, "1 to 65535"),
        ({k: v for k, v in NEW.items() if k != "username"}, "missing: username"),
        ({**NEW, "readonly_password": "x"}, "needs a read-only user"),
        ({**NEW, "colour": "red"}, "unknown field"),
    ],
)
def test_what_cannot_be_added(service, fields, message):
    with pytest.raises(ManagedDatabaseError, match=message):
        service.prepare(fields, by=None)


def test_a_name_taken_meanwhile_is_refused_when_kept(service):
    database = service.prepare(NEW, by=None)
    service.add(service.prepare(NEW, by=None))
    with pytest.raises(ManagedDatabaseError, match="already exists"):
        service.add(database)


def test_unknown_ones_are_not_found(service):
    with pytest.raises(LookupError):
        service.get("nope")
    with pytest.raises(LookupError):
        service.remove("nope")
    assert service.is_configured("shop") and not service.is_configured("sales")
