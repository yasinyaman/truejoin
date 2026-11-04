"""Fusion ports: the interfaces adapters implement and services depend on.

Ports are ``typing.Protocol`` classes expressed purely in domain types, so
any adapter (DuckDB, Warp, an in-memory fake, ...) can satisfy them without
the application layer knowing which one is plugged in.
"""

from trio.ports.analytics_store import AnalyticsStore
from trio.ports.cache import QueryCache
from trio.ports.data_source import (
    DatabaseDiscovery,
    DataSource,
    PushdownCapable,
    SourceFactory,
)
from trio.ports.scheduler import ScheduledJob, Scheduler
from trio.ports.semantic_compiler import SemanticCompiler
from trio.ports.sql_policy import SqlAnalyzer, SqlValidator

__all__ = [
    "AnalyticsStore",
    "DataSource",
    "DatabaseDiscovery",
    "PushdownCapable",
    "QueryCache",
    "ScheduledJob",
    "Scheduler",
    "SemanticCompiler",
    "SourceFactory",
    "SqlAnalyzer",
    "SqlValidator",
]
