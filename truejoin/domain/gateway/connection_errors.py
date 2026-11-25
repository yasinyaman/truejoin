"""Why a database connection failed, in words that say what to check.

A driver says why it could not connect in its own way: asyncpg raises
``InvalidPasswordError``, aiomysql an ``OperationalError`` with code 1045, an
ODBC driver a state and a native code inside a sentence, the operating system
``ECONNREFUSED``. ``classify`` reads the exception and its causes (types by
name, codes, then the text) and gives one reason and one piece of advice, so a
person adding a database is told to check the port rather than shown
``[Errno 111] Connection refused``.

Pure: no driver is imported, so the drivers' classes are matched by name. The
advice is composed from the host, port and database name only, never from the
driver's text, which can echo a password; that text is still shown, scrubbed,
by the caller.
"""

from __future__ import annotations

import re
import socket
import ssl
from collections.abc import Iterator
from dataclasses import dataclass

#: Every reason ``classify`` gives; ``unknown`` keeps the driver's own text.
REASONS = (
    "refused",
    "unreachable",
    "unknown_host",
    "timeout",
    "credentials",
    "not_allowed",
    "database",
    "tls",
    "driver",
    "unknown",
)

_BY_CLASS = {
    "InvalidPasswordError": "credentials",
    "InvalidAuthorizationSpecificationError": "credentials",
    "InvalidCatalogNameError": "database",
}
#: MySQL and MariaDB client and server codes, the first argument of their errors.
_BY_CODE = {
    1044: "credentials",
    1045: "credentials",
    1049: "database",
    2003: "refused",
    2005: "unknown_host",
}
#: Read in order: a sentence can name more than one thing ("SSL ... login failed").
_BY_TEXT: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (reason, re.compile(pattern, re.IGNORECASE))
    for reason, pattern in (
        (
            "credentials",
            r"password authentication failed|access denied for user|login failed for user|ora-01017|invalid username/password|\b18456\b",
        ),
        ("not_allowed", r"pg_hba\.conf|host '[^']*' is not allowed to connect"),
        (
            "database",
            r"database \S+ does not exist|unknown database|cannot open database|ora-12514|\b4060\b",
        ),
        (
            "driver",
            r"\bim002\b|can't open lib|data source name not found|libclntsh|no such file.*odbc",
        ),
        ("tls", r"certificate|\bssl\b|\btls\b"),
        (
            "unknown_host",
            r"name or service not known|nodename nor servname|getaddrinfo failed|could not translate host name|temporary failure in name resolution|\b11001\b",
        ),
        ("timeout", r"timed out|timeout expired|\bhyt00\b"),
        ("unreachable", r"no route to host|network is unreachable|errno 113\b|errno 101\b"),
        ("refused", r"connection refused|errno 111\b|ora-12541|\b10061\b"),
    )
)


@dataclass(frozen=True, slots=True)
class ConnectionProblem:
    """What went wrong, as one of ``REASONS``, and what to check (empty for unknown)."""

    reason: str
    advice: str


def classify(
    error: BaseException, host: str = "", port: int | None = None, database: str = ""
) -> ConnectionProblem:
    """The reason ``error`` (raised while connecting) stands for, with advice."""
    reason = _reason(error)
    return ConnectionProblem(reason, _advice(reason, host, port, database))


def _chain(error: BaseException) -> Iterator[BaseException]:
    """``error`` and what it was raised from, each once."""
    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen and len(seen) < 10:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _reason(error: BaseException) -> str:
    chain = list(_chain(error))
    for e in chain:
        if isinstance(e, ConnectionRefusedError):
            return "refused"
        if isinstance(e, socket.gaierror):
            return "unknown_host"
        if isinstance(e, ssl.SSLError):
            return "tls"
        if isinstance(e, TimeoutError):
            return "timeout"
        for cls in type(e).__mro__:
            if cls.__name__ in _BY_CLASS:
                return _BY_CLASS[cls.__name__]
        code = e.args[0] if e.args else None
        if isinstance(code, int) and not isinstance(code, bool) and code in _BY_CODE:
            return _BY_CODE[code]
    text = " ".join(str(e) for e in chain)
    return next((reason for reason, pattern in _BY_TEXT if pattern.search(text)), "unknown")


def _advice(reason: str, host: str, port: int | None, database: str) -> str:
    where = f"{host}:{port}" if host and port else host or "the server"
    return {
        "refused": f"nothing answers at {where}: check the host and port, and that the "
        f"database server is running and accepts network connections",
        "unreachable": f"{host or 'the host'} cannot be reached from this server: check the "
        f"network path and any firewall between them",
        "unknown_host": f"the host name '{host}' could not be found: check its spelling, "
        f"or use an IP address",
        "timeout": f"{where} did not answer in time: a firewall may be dropping the "
        f"connection, or the server is busy",
        "credentials": "the database refused the user name or password",
        "not_allowed": "the database server does not accept this user from this server's "
        "address (PostgreSQL: a pg_hba.conf entry; MySQL: the user's host)",
        "database": f"the server has no database '{database}', or this user may not open it",
        "tls": "the encrypted connection failed: check the SSL setting and the server's "
        "certificate (for SQL Server, trust_server_certificate in the other options)",
        "driver": "the ODBC driver this type needs is not installed on this server",
        "unknown": "",
    }[reason]
