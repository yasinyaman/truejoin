"""Tests for the audit sinks."""

import json
import logging

from trio.adapters.outbound.audit import LoggingAuditSink
from trio.adapters.outbound.audit.log_sink import AUDIT_LOGGER
from trio.domain.governance.audit import AuditEvent
from trio.ports.audit import NullAuditSink

EVENT = AuditEvent(
    action="read",
    database="shop",
    table="users",
    actor="acme",
    request_id="r1",
    row_count=2,
    masked_columns=("email",),
)


def test_the_null_sink_records_nothing():
    NullAuditSink().record(EVENT)  # must not raise


class TestLoggingSink:
    def test_it_logs_to_a_dedicated_logger(self, caplog):
        # A separate logger so audit output can be routed and retained apart
        # from application logs, without filtering on message text.
        with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
            LoggingAuditSink().record(EVENT)
        assert caplog.records
        assert caplog.records[0].name == AUDIT_LOGGER
        assert caplog.records[0].extra_fields["table"] == "users"

    def test_it_appends_json_lines_to_a_file(self, tmp_path):
        path = tmp_path / "audit" / "trail.jsonl"
        sink = LoggingAuditSink(str(path))
        sink.record(EVENT)
        sink.record(EVENT)

        lines = path.read_text().strip().splitlines()
        assert len(lines) == 2
        first = json.loads(lines[0])
        assert first["event"] == "data_access"
        assert first["actor"] == "acme"
        assert first["masked_columns"] == ["email"]

    def test_the_file_is_appended_not_rewritten(self, tmp_path):
        # A restart must not truncate the trail.
        path = tmp_path / "trail.jsonl"
        LoggingAuditSink(str(path)).record(EVENT)
        LoggingAuditSink(str(path)).record(EVENT)
        assert len(path.read_text().strip().splitlines()) == 2

    def test_the_parent_directory_is_created(self, tmp_path):
        path = tmp_path / "nested" / "deeper" / "trail.jsonl"
        LoggingAuditSink(str(path)).record(EVENT)
        assert path.exists()

    def test_an_unwritable_path_does_not_fail_the_request(self, tmp_path, caplog):
        # An audit sink that can fail a request turns a logging problem into
        # an outage.
        blocked = tmp_path / "file.txt"
        blocked.write_text("not a directory")
        sink = LoggingAuditSink(str(blocked / "trail.jsonl"))
        with caplog.at_level(logging.ERROR):
            sink.record(EVENT)
        assert "audit" in caplog.text.lower()

    def test_no_file_configured_still_logs(self, caplog):
        with caplog.at_level(logging.INFO, logger=AUDIT_LOGGER):
            LoggingAuditSink().record(EVENT)
        assert caplog.records


class TestJsonlReader:
    """The trail read back newest first, as the console's Audit tab shows it."""

    def _trail(self, tmp_path, events):
        path = tmp_path / "audit.jsonl"
        sink = LoggingAuditSink(str(path))
        for event in events:
            sink.record(event)
        return path

    def test_newest_first_filtered_and_limited(self, tmp_path):
        from dataclasses import replace

        from trio.adapters.outbound.audit import JsonlAuditReader

        path = self._trail(
            tmp_path,
            [
                replace(EVENT, request_id="r1"),
                replace(EVENT, request_id="r2", actor="other", masked_columns=()),
                replace(EVENT, request_id="r3", action="update", change="approve_table"),
            ],
        )
        with path.open("a") as handle:
            handle.write("not json, a line cut short by a crash\n")
        reader = JsonlAuditReader(str(path))
        events, complete = reader.recent(10, {})
        assert complete and [e["request_id"] for e in events] == ["r3", "r2", "r1"]
        assert [e["request_id"] for e in reader.recent(1, {})[0]] == ["r3"]
        assert [e["request_id"] for e in reader.recent(10, {"actor": "ACME"})[0]] == ["r3", "r1"]
        assert [e["request_id"] for e in reader.recent(10, {"restricted": "True"})[0]] == [
            "r3",
            "r1",
        ]
        assert [e["request_id"] for e in reader.recent(10, {"event": "catalog_change"})[0]] == [
            "r3"
        ]

    def test_only_the_newest_part_of_a_long_file_is_searched(self, tmp_path):
        from dataclasses import replace

        from trio.adapters.outbound.audit import JsonlAuditReader

        path = self._trail(tmp_path, [replace(EVENT, request_id=f"r{i}") for i in range(20)])
        events, complete = JsonlAuditReader(str(path), scan_bytes=1500).recent(100, {})
        assert not complete
        assert events and events[0]["request_id"] == "r19"
        assert all(isinstance(e, dict) for e in events)  # the cut first line is skipped

    def test_no_file_yet_is_an_empty_trail(self, tmp_path):
        from trio.adapters.outbound.audit import JsonlAuditReader

        assert JsonlAuditReader(str(tmp_path / "none.jsonl")).recent(5, {}) == ([], True)


def test_filter_columns_can_be_left_out_of_every_event(tmp_path):
    import json

    from trio.domain.governance.audit import AuditEvent, ToolCall

    path = tmp_path / "trail.jsonl"
    sink = LoggingAuditSink(str(path), filter_columns=False)
    sink.record(AuditEvent(action="read", database="db", table="t", filtered_columns=("email",)))
    sink.record(ToolCall(tool="query_data", outcome="answered", filtered_columns=("durum",)))
    written = [json.loads(line) for line in path.read_text().splitlines()]
    assert [e["filtered_columns"] for e in written] == [[], []]
    LoggingAuditSink(str(path)).record(
        AuditEvent(action="read", database="db", table="t", filtered_columns=("email",))
    )
    assert json.loads(path.read_text().splitlines()[-1])["filtered_columns"] == ["email"]
