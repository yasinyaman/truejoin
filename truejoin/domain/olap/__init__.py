"""Fusion domain layer: pure Python models, rules and errors.

Nothing in this package may import third-party code or perform I/O. The
architecture test in ``tests/architecture`` enforces that rule.
"""

from truejoin.domain.olap.catalog import SchemaCatalog, SourceEntry
from truejoin.domain.olap.errors import (
    BackupError,
    CacheError,
    CircuitOpenError,
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
    ListRowStream,
    QueryResult,
    RefreshSpec,
    RowSet,
    RowStream,
    SourceCapabilities,
    SourceSchema,
    TableRef,
    TableSchema,
    TableSize,
)
from truejoin.domain.olap.policy import (
    MaterializationPolicy,
    SemiJoinSpec,
    TargetPlan,
)
from truejoin.domain.olap.query_shape import JoinEquality, QueryShape, TableUse
from truejoin.domain.olap.slices import (
    LoadedSlice,
    Predicate,
    SliceRegistry,
    SliceSpec,
)

__all__ = [
    "BackupError",
    "BackupInfo",
    "CacheError",
    "CircuitOpenError",
    "ColumnInfo",
    "ConnectionError",
    "FetchPlan",
    "FusionError",
    "GuardrailViolation",
    "JoinEquality",
    "ListRowStream",
    "LoadedSlice",
    "MaterializationPolicy",
    "Predicate",
    "QueryError",
    "QueryResult",
    "QueryShape",
    "RefreshSpec",
    "RowSet",
    "RowStream",
    "SchemaCatalog",
    "SchemaError",
    "SemiJoinSpec",
    "SliceRegistry",
    "SliceSpec",
    "SourceCapabilities",
    "SourceEntry",
    "SourceSchema",
    "TableRef",
    "TableSchema",
    "TableSize",
    "TableUse",
    "TargetPlan",
]
