"""Tests for the audit event."""

import json
from datetime import UTC, datetime

import pytest

from truejoin.domain.governance.audit import WRITE_ACTIONS, AuditEvent


def event(**overrides) -> AuditEvent:
    base = {
        "action": "read",
        "database": "shop",
        "table": "users",
        "actor": "acme-reader",
        "tenant": "acme",
        "roles": ("support",),
        "request_id": "abc123",
        "row_count": 3,
    }
    return AuditEvent(**{**base, **overrides})


class TestClassification:
    @pytest.mark.parametrize("action", ["create", "update", "delete"])
    def test_writes_are_marked(self, action):
        assert event(action=action).is_write

    @pytest.mark.parametrize("action", ["read", "export", "query"])
    def test_reads_are_not(self, action):
        assert not event(action=action).is_write

    def test_the_write_set_matches(self):
        assert {"create", "update", "delete"} == WRITE_ACTIONS

    def test_restricted_means_something_was_withheld(self):
        assert not event().restricted
        assert event(row_filtered=True).restricted
        assert event(masked_columns=("email",)).restricted


class TestSerialization:
    def test_as_dict_is_json_safe(self):
        payload = event(at=datetime(2024, 3, 9, 12, 0, tzinfo=UTC)).as_dict()
        assert json.loads(json.dumps(payload))["at"].startswith("2024-03-09T12:00:00")

    def test_it_records_who_what_and_whether_it_was_restricted(self):
        payload = event(row_filtered=True, masked_columns=("email",)).as_dict()
        assert payload["actor"] == "acme-reader"
        assert payload["tenant"] == "acme"
        assert payload["roles"] == ["support"]
        assert payload["action"] == "read"
        assert payload["database"] == "shop"
        assert payload["table"] == "users"
        assert payload["row_count"] == 3
        assert payload["request_id"] == "abc123"
        # The question an audit log is actually asked afterwards.
        assert payload["row_filtered"] is True
        assert payload["masked_columns"] == ["email"]
        assert payload["restricted"] is True

    def test_an_unattributed_request_is_recorded_as_such(self):
        # None is itself worth recording: the request could not be attributed.
        assert event(actor=None, tenant=None, roles=()).as_dict()["actor"] is None

    def test_no_row_values_are_carried_anywhere(self):
        # Filter *columns*, never the values compared against them: an audit
        # trail that quotes the data becomes a second copy of it.
        payload = event(filtered_columns=("status", "tenant_id")).as_dict()
        assert payload["filtered_columns"] == ["status", "tenant_id"]
        assert "value" not in json.dumps(payload)
        assert "values" not in payload

    def test_the_event_has_no_field_that_could_hold_a_value(self):
        fields = set(event().as_dict())
        assert not fields & {"sql", "params", "parameters", "rows", "data", "body"}


class TestSummary:
    def test_a_plain_read(self):
        assert event(row_count=3).summary() == "acme-reader read shop.users rows=3 status=200"

    def test_restrictions_are_visible_at_a_glance(self):
        line = event(row_filtered=True, masked_columns=("email", "phone")).summary()
        assert "row-filtered" in line
        assert "masked=email,phone" in line

    def test_an_unattributed_request_reads_as_anonymous(self):
        assert event(actor=None).summary().startswith("anonymous ")

    def test_a_missing_row_count_is_omitted(self):
        assert "rows=" not in event(row_count=None).summary()


class TestFingerprint:
    def test_it_is_on_the_event_only_when_set(self):
        assert "sql_fingerprint" not in event().as_dict()
        assert event(sql_fingerprint="ab" * 16).as_dict()["sql_fingerprint"] == "ab" * 16

    def test_it_is_keyed(self):
        from truejoin.domain.governance.audit import fingerprint_key, sql_fingerprint

        one, other = fingerprint_key("secret-one"), fingerprint_key("secret-two")
        assert len(sql_fingerprint("SELECT ?", one)) == 32
        assert sql_fingerprint("SELECT ?", one) == sql_fingerprint("SELECT ?", one)
        assert sql_fingerprint("SELECT ?", one) != sql_fingerprint("SELECT ?", other)
        assert sql_fingerprint("", one) == ""


class TestSignIn:
    def test_an_attempt_carries_how_who_and_from_where_only(self):
        from truejoin.domain.governance.audit import SignIn

        payload = SignIn("password", "refused", actor="admin", client="10.0.0.1").as_dict()
        assert payload["event"] == "sign_in" and payload["outcome"] == "refused"
        assert not set(payload) & {"password", "key", "token", "secret"}
