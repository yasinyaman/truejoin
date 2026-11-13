"""Fusion — DuckDB-powered in-memory OLAP engine with LLM tool support.

Quick start::

    from truejoin import Settings, build_app

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

from truejoin import ports
from truejoin.application.olap.app import FusionApp
from truejoin.application.olap.settings import Settings
from truejoin.application.olap.tool_schemas import (
    TOOL_DEFINITIONS,
    TOOL_NAMES,
    get_mcp_tools,
    get_openai_tools,
)
from truejoin.domain.olap.catalog import SchemaCatalog
from truejoin.domain.olap.errors import (
    BackupError,
    CacheError,
    ConnectionError,
    FusionError,
    GuardrailViolation,
    QueryError,
    SchemaError,
)
from truejoin.domain.olap.models import (
    BackupInfo,
    ColumnInfo,
    FetchPlan,
    QueryResult,
    RowSet,
    TableRef,
    TableSchema,
)

if TYPE_CHECKING:
    from truejoin.bootstrap import build_app

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
        from truejoin.bootstrap import build_app

        return build_app
    raise AttributeError(f"module 'fusion' has no attribute {name!r}")
