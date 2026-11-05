"""Fusion — DuckDB-powered in-memory OLAP engine with LLM tool support.

Quick start::

    from trio import Settings, build_app

    app = build_app(Settings(memory_limit="4GB"))
    app.sources.connect("mydb", {"type": "warp-remote", "base_url": "http://localhost:8000"})
    app.tools.query_data("SELECT * FROM mydb.orders LIMIT 10")
    app.close()

Importing ``fusion`` is cheap: only the pure domain and application layers
load eagerly; adapters (DuckDB, sqlglot, requests, ...) are imported when
``build_app`` is first called.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from trio import ports
from trio.application.olap.app import FusionApp
from trio.application.olap.settings import Settings
from trio.application.olap.tool_schemas import (
    TOOL_DEFINITIONS,
    TOOL_NAMES,
    get_mcp_tools,
    get_openai_tools,
)
from trio.domain.olap.catalog import SchemaCatalog
from trio.domain.olap.errors import (
    BackupError,
    CacheError,
    ConnectionError,
    FusionError,
    GuardrailViolation,
    QueryError,
    SchemaError,
)
from trio.domain.olap.models import (
    BackupInfo,
    ColumnInfo,
    FetchPlan,
    QueryResult,
    RowSet,
    TableRef,
    TableSchema,
)

if TYPE_CHECKING:
    from trio.bootstrap import build_app

__version__ = "1.0.0"

__all__ = [
    "TOOL_DEFINITIONS",
    "TOOL_NAMES",
    "BackupError",
    "BackupInfo",
    "CacheError",
    "ColumnInfo",
    "ConnectionError",
    "FetchPlan",
    "FusionApp",
    "FusionError",
    "GuardrailViolation",
    "QueryError",
    "QueryResult",
    "RowSet",
    "SchemaCatalog",
    "SchemaError",
    "Settings",
    "TableRef",
    "TableSchema",
    "__version__",
    "build_app",
    "get_mcp_tools",
    "get_openai_tools",
    "ports",
]


def __getattr__(name: str) -> Any:
    # Lazy so `import fusion` never pulls in DuckDB/sqlglot/requests.
    if name == "build_app":
        from trio.bootstrap import build_app

        return build_app
    raise AttributeError(f"module 'fusion' has no attribute {name!r}")
