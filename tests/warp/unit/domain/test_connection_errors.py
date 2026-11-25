"""A driver's connection failure, read as a reason and advice: no driver is imported."""

import socket
import ssl

import pytest

from truejoin.domain.gateway.connection_errors import REASONS, classify
from truejoin.domain.gateway.managed import explain_failure


# Named like the drivers' own classes: classify matches them by name.
class InvalidPasswordError(Exception):
    pass


class InvalidCatalogNameError(Exception):
    pass


class OperationalError(Exception):
    pass


class Error(Exception):  # pyodbc's
    pass


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (ConnectionRefusedError(111, "Connection refused"), "refused"),
        (
            OSError("Multiple exceptions: [Errno 111] Connect call failed ('127.0.0.1', 5999)"),
            "refused",
        ),
        (socket.gaierror(-2, "Name or service not known"), "unknown_host"),
        (TimeoutError(), "timeout"),
        (ssl.SSLError("WRONG_VERSION_NUMBER"), "tls"),
        (InvalidPasswordError('password authentication failed for user "u"'), "credentials"),
        (InvalidCatalogNameError('database "crm" does not exist'), "database"),
        (
            Exception('no pg_hba.conf entry for host "10.0.0.5", user "u", database "d", SSL off'),
            "not_allowed",
        ),
        (
            OperationalError(1045, "Access denied for user 'u'@'10.0.0.5' (using password: YES)"),
            "credentials",
        ),
        (OperationalError(1049, "Unknown database 'crm'"), "database"),
        (
            OperationalError(
                2003, "Can't connect to MySQL server on 'db' ([Errno 111] Connection refused)"
            ),
            "refused",
        ),
        (
            Error(
                "28000",
                "[28000] [Microsoft][ODBC Driver 18 for SQL Server][SQL Server]Login failed for user 'sa'. (18456)",
            ),
            "credentials",
        ),
        (
            Error("42000", '[42000] Cannot open database "crm" requested by the login. (4060)'),
            "database",
        ),
        (
            Error(
                "08001",
                "[08001] SSL Provider: The certificate chain was issued by an authority that is not trusted.",
            ),
            "tls",
        ),
        (
            Error(
                "01000",
                "[01000] [unixODBC][Driver Manager]Can't open lib 'ODBC Driver 18 for SQL Server'",
            ),
            "driver",
        ),
        (Error("HY000", "ORA-01017: invalid username/password; logon denied"), "credentials"),
        (Error("HY000", "ORA-12541: TNS:no listener"), "refused"),
        (
            Error("HY000", "ORA-12514: TNS:listener does not currently know of service requested"),
            "database",
        ),
        (Exception("something else entirely"), "unknown"),
    ],
)
def test_each_driver_failure_has_its_reason(error, reason):
    assert reason in REASONS
    assert classify(error).reason == reason


def test_the_cause_is_read_when_the_error_only_wraps_it():
    try:
        try:
            raise InvalidPasswordError("password authentication failed")
        except InvalidPasswordError as inner:
            raise RuntimeError("pool could not start") from inner
    except RuntimeError as outer:
        assert classify(outer).reason == "credentials"


def test_the_advice_names_where_and_never_the_password():
    error = InvalidPasswordError(
        'password authentication failed for user "u" (password "s3cret-pw")'
    )
    explained = explain_failure(
        error, "could not connect", host="db", port=5432, database="crm", secrets=["s3cret-pw"]
    )
    assert explained.reason == "credentials"
    assert "s3cret-pw" not in str(explained) and "s3cret-pw" not in explained.raw
    assert str(explained).startswith("could not connect: the database refused the user name")
    refused = explain_failure(
        ConnectionRefusedError(111, "x"), "could not connect", host="db", port=5999
    )
    assert "nothing answers at db:5999" in str(refused)


def test_a_failure_it_cannot_read_keeps_the_drivers_words():
    explained = explain_failure(Exception("odd failure"), "could not connect")
    assert (explained.reason, str(explained)) == ("unknown", "could not connect: odd failure")
    wrapped = explain_failure(
        Exception("gave up"), "not connected", text="[Errno 111] Connection refused"
    )
    assert wrapped.reason == "refused"
