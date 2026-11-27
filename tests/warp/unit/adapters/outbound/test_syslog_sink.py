"""The syslog copy of the audit trail, against real sockets on this machine."""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import socket
import ssl
import threading
import time
from pathlib import Path

import pytest

from truejoin.adapters.outbound.audit.log_sink import FanOutAuditSink
from truejoin.adapters.outbound.audit.syslog_sink import SyslogAuditSink, severity, to_cef
from truejoin.domain.governance.audit import AuditEvent, SignIn, ToolCall

READ = AuditEvent(
    action="query",
    database="shop",
    table="orders",
    actor="alice",
    tenant="a",
    roles=("analyst",),
    request_id="req-1",
    row_count=3,
    row_filtered=True,
    masked_columns=("email",),
    filtered_columns=("status",),
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _until(condition, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("timed out")
        time.sleep(0.02)


class TcpReceiver:
    """Accepts one connection at a time and splits octet-counted frames."""

    def __init__(self, port: int = 0, context: ssl.SSLContext | None = None) -> None:
        self.server = socket.socket()
        self.server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server.bind(("127.0.0.1", port))
        self.server.listen()
        self.port = int(self.server.getsockname()[1])
        self.context = context
        self.messages: list[bytes] = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        try:
            conn, _ = self.server.accept()
        except OSError:
            return
        if self.context is not None:
            try:
                conn = self.context.wrap_socket(conn, server_side=True)
            except (ssl.SSLError, OSError):
                conn.close()  # the client refused our certificate: nothing to read
                return
        buffer = b""
        with conn:
            while True:
                try:
                    chunk = conn.recv(65536)
                except OSError:
                    return
                if not chunk:
                    return
                buffer += chunk
                while b" " in buffer:
                    length, _, rest = buffer.partition(b" ")
                    if len(rest) < int(length):
                        break
                    self.messages.append(rest[: int(length)])
                    buffer = rest[int(length) :]

    def close(self) -> None:
        self.server.close()


def _message(raw: bytes) -> tuple[str, dict]:
    """``<PRI>1 TIMESTAMP HOST APP PROCID MSGID SD MSG`` -> (the header, the JSON message)."""
    *head, message = raw.decode().split(" ", 7)
    return " ".join(head) + " ", json.loads(message)


class TestUdp:
    def test_an_event_arrives_as_rfc5424_json(self):
        receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(5)
        sink = SyslogAuditSink("127.0.0.1", receiver.getsockname()[1], app_name="tj")
        try:
            sink.record(READ)
            raw, _ = receiver.recvfrom(65536)
        finally:
            sink.close()
            receiver.close()
        head, payload = _message(raw)
        assert head.startswith("<134>1 ")  # local0 (16) * 8 + info (6)
        assert " tj " in head
        assert payload["event"] == "data_access" and payload["actor"] == "alice"
        assert payload["masked_columns"] == ["email"] and payload["filtered_columns"] == ["status"]

    def test_the_filtered_columns_follow_the_trails_setting(self):
        receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(5)
        sink = SyslogAuditSink("127.0.0.1", receiver.getsockname()[1], filter_columns=False)
        try:
            sink.record(READ)
            raw, _ = receiver.recvfrom(65536)
        finally:
            sink.close()
            receiver.close()
        assert _message(raw)[1]["filtered_columns"] == []


class TestTcp:
    def test_frames_are_octet_counted(self):
        receiver = TcpReceiver()
        sink = SyslogAuditSink("127.0.0.1", receiver.port, protocol="tcp")
        try:
            sink.record(READ)
            sink.record(ToolCall(tool="query_data", outcome="answered", actor="bob"))
            _until(lambda: len(receiver.messages) == 2)
        finally:
            sink.close()
            receiver.close()
        assert _message(receiver.messages[1])[1]["tool"] == "query_data"

    def test_a_receiver_that_is_down_at_start_gets_the_event_once_it_is_up(self):
        port = _free_port()
        sink = SyslogAuditSink("127.0.0.1", port, protocol="tcp")
        try:
            sink.record(READ)  # nothing listens: queued, never raised
            time.sleep(0.2)
            receiver = TcpReceiver(port)
            _until(lambda: len(receiver.messages) == 1, timeout=8)
        finally:
            sink.close(timeout=0.5)
        receiver.close()
        assert _message(receiver.messages[0])[1]["request_id"] == "req-1"

    def test_a_full_queue_drops_and_counts_never_blocks(self):
        sink = SyslogAuditSink("127.0.0.1", _free_port(), protocol="tcp", queue_size=1)
        try:
            started = time.monotonic()
            for _ in range(20):
                sink.record(READ)
            assert time.monotonic() - started < 0.5
            assert sink.dropped >= 18
        finally:
            sink.close(timeout=0.2)


def _certificate(tmp_path: Path) -> tuple[Path, Path]:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=1))
        .not_valid_after(now + dt.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_file, key_file = tmp_path / "cert.pem", tmp_path / "key.pem"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_file, key_file


class TestTls:
    def test_an_event_arrives_over_tls_checked_against_the_ca(self, tmp_path):
        cert, key = _certificate(tmp_path)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        receiver = TcpReceiver(context=context)
        sink = SyslogAuditSink("127.0.0.1", receiver.port, protocol="tls", ca_file=str(cert))
        try:
            sink.record(
                SignIn(method="password", outcome="refused", actor="admin", client="10.0.0.9")
            )
            _until(lambda: len(receiver.messages) == 1)
        finally:
            sink.close()
            receiver.close()
        head, payload = _message(receiver.messages[0])
        assert head.startswith("<132>1 ")  # a refused sign-in is a warning
        assert payload["event"] == "sign_in" and payload["client"] == "10.0.0.9"
        assert set(payload) == {
            "event", "at", "action", "method", "outcome", "actor", "client", "request_id", "reason",
        }  # fmt: skip

    def test_a_certificate_the_ca_did_not_sign_is_not_sent_to(self, tmp_path):
        cert, key = _certificate(tmp_path)
        (tmp_path / "other").mkdir()
        other, _ = _certificate(tmp_path / "other")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        receiver = TcpReceiver(context=context)
        sink = SyslogAuditSink("127.0.0.1", receiver.port, protocol="tls", ca_file=str(other))
        try:
            sink.record(READ)
            time.sleep(0.5)
            assert receiver.messages == [] and sink.sent == 0
        finally:
            sink.close(timeout=0.2)
            receiver.close()


class TestCef:
    def test_a_line_carries_the_fields_and_escapes_what_cef_reserves(self):
        event = AuditEvent(
            action="update",
            database="db",
            table="t|x",
            actor="a=b\\c\nd",
            change="approve_table",
            subject="catalog",
            sql_fingerprint="f" * 32,
        )
        line = to_cef(event.as_dict(), "1.0.0", "host1")
        assert line.startswith("CEF:0|Truejoin|Truejoin|1.0.0|catalog_change.update|")
        assert "|5|" in line  # a catalog change is a notice
        assert "suser=a\\=b\\\\c\\nd" in line
        assert "cs2=t|x" in line and "cs2Label=table" in line
        assert "cs6=" + "f" * 32 in line and "dvchost=host1" in line
        header = line.split("|")
        assert len(header) >= 8

    def test_an_assistants_run_and_its_calls_can_be_told_apart(self):
        from truejoin.domain.governance.audit import AssistantRun, ToolCall

        run = AssistantRun(
            "exhausted", actor="a", provider="ollama", model="m", steps=8, tool_calls=7
        )
        line = to_cef(run.as_dict(), "1.0.0", "h")
        assert "|assistant_run.assistant|" in line and "act=assistant" in line
        assert "flexNumber1=8 flexNumber1Label=steps" in line
        assert "flexNumber2=7 flexNumber2Label=tool_calls" in line
        assert severity(run.as_dict()) == (6, 3)  # an answer, though a late one
        assert severity(AssistantRun("failed").as_dict()) == (4, 7)
        call = ToolCall("query_data", "answered", actor="a", via="assistant")
        assert "requestClientApplication=assistant" in to_cef(call.as_dict(), "1.0.0", "h")

    def test_severities(self):
        assert severity(READ.as_dict()) == (6, 3)
        assert severity({**READ.as_dict(), "status": 403}) == (4, 7)
        assert severity(AuditEvent("delete", "db", "t").as_dict()) == (5, 5)

    def test_the_json_and_cef_messages_never_hold_sql(self):
        sink = SyslogAuditSink("127.0.0.1", _free_port(), fmt="cef")
        try:
            frame = sink.encode(READ.as_dict()).decode()
        finally:
            sink.close(timeout=0.1)
        assert "SELECT" not in frame and "CEF:0|" in frame


class TestFanOut:
    def test_every_sink_gets_the_event_and_closing_closes_them(self):
        got: list[str] = []

        class Sink:
            closed = False

            def record(self, event):
                got.append(event.action)

            def close(self):
                Sink.closed = True

        class Broken:
            def record(self, event):
                raise RuntimeError("down")

        fan = FanOutAuditSink([Broken(), Sink()])
        fan.record(READ)
        fan.close()
        assert got == ["query"] and Sink.closed


def test_a_bad_setting_is_refused():
    with pytest.raises(ValueError, match="protocol"):
        SyslogAuditSink("127.0.0.1", 514, protocol="smoke")
