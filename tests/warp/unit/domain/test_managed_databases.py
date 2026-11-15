"""The rules a database added from the console is held to."""

import pytest

from truejoin.domain.gateway.managed import (
    ManagedDatabase,
    ManagedDatabaseError,
    check_new_name,
    scrub,
)

SALES = ManagedDatabase(
    name="sales", type="postgresql", database="sales", username="app", password="s3cret"
)


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("bad-name", "cannot be a database name"),
        ("1st", "cannot be a database name"),
        ("a.b", "cannot be a database name"),
        ("Keys", "server's own routes"),
        ("databases", "server's own routes"),
        # /settings and /auth/users: a database called auth would lose its users table
        ("settings", "server's own routes"),
        ("Auth", "server's own routes"),
        ("SHOP", "already exists"),
    ],
)
def test_a_name_is_a_schema_and_a_url_segment(name, message):
    with pytest.raises(ManagedDatabaseError, match=message):
        check_new_name(name, ["shop"])
    check_new_name("sales_2", ["shop"])


def test_a_blank_password_keeps_the_one_kept():
    changed = SALES.changed({"password": "", "port": 6543}, by="admin")
    assert (changed.password, changed.port, changed.updated_by) == ("s3cret", 6543, "admin")
    assert SALES.changed({"password": "new"}, by=None).password == "new"


def test_a_database_is_not_renamed_in_place():
    with pytest.raises(ManagedDatabaseError, match="renamed"):
        SALES.changed({"name": "other"}, by=None)
    with pytest.raises(ManagedDatabaseError, match="cannot change"):
        SALES.changed({"created_by": "me"}, by=None)


def test_what_is_shown_has_no_password():
    shown = SALES.public()
    assert "s3cret" not in str(shown)
    assert shown["has_password"] is True and shown["has_readonly_password"] is False


def test_a_record_round_trips_and_the_port_defaults_by_type():
    assert ManagedDatabase.from_record(SALES.as_record()) == SALES
    record = {"name": "m", "type": "mysql", "database": "d", "username": "u"}
    assert ManagedDatabase.from_record(record).port == 3306


def test_a_drivers_message_loses_the_password():
    message = "login failed for PWD=s3cret;UID=app with password s3cret"
    assert "s3cret" not in scrub(message, ["s3cret", ""])
    assert scrub("Server=x;PWD=other;", []) == "Server=x;PWD=***;"
