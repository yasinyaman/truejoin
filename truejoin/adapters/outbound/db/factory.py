"""Database factory for creating database adapters."""

from collections.abc import Mapping
from typing import Any

from truejoin.adapters.outbound.db.base import DatabaseAdapter
from truejoin.adapters.outbound.db.mysql import MySQLAdapter
from truejoin.adapters.outbound.db.odbc import ODBCAdapter
from truejoin.adapters.outbound.db.oracle import OracleAdapter
from truejoin.adapters.outbound.db.postgres import PostgreSQLAdapter
from truejoin.application.settings import DatabaseConfig

# Registry of available database adapters
ADAPTERS: dict[str, type[DatabaseAdapter]] = {
    "postgresql": PostgreSQLAdapter,
    "postgres": PostgreSQLAdapter,
    "mysql": MySQLAdapter,
    "mariadb": MySQLAdapter,
    # Oracle through python-oracledb thin mode; through ODBC when the config
    # names an ODBC driver or connection string (see `uses_odbc`).
    "oracle": OracleAdapter,
    # ODBC (aioodbc/pyodbc): SQL Server, plus a generic best-effort profile for
    # every other ODBC data source.
    "mssql": ODBCAdapter,
    "sqlserver": ODBCAdapter,
    "odbc": ODBCAdapter,
}


def uses_odbc(config: Mapping[str, Any]) -> bool:
    """Whether an Oracle config asks for ODBC: it names a driver or a connection string.

    Those two options mean nothing to python-oracledb, and every Oracle config
    written for the ODBC adapter carries one of them, so it keeps working.
    """
    options: Mapping[str, Any] = config.get("options") or {}
    return bool(options.get("driver") or options.get("connection_string"))


class DatabaseFactory:
    """Factory class for creating database adapters.

    Usage:
        adapter = DatabaseFactory.create(config)
        await adapter.connect()

    To add a new database type:
        1. Create a new adapter class inheriting from DatabaseAdapter
        2. Register it in the ADAPTERS dictionary
    """

    @staticmethod
    def create(config: DatabaseConfig | dict[str, Any]) -> DatabaseAdapter:
        """Create a database adapter based on configuration.

        Args:
            config: A `DatabaseConfig` (preferred; `options` stay nested so pool
                   sizing/SSL reach the adapter) or the equivalent dictionary with
                   `type`, `host`, `port`, `database`, `username`, `password`
                   and an optional nested `options` mapping.

        Returns:
            DatabaseAdapter instance.

        Raises:
            ValueError: If database type is not supported.
        """
        if isinstance(config, DatabaseConfig):
            config = config.model_dump()
        db_type = config.get("type", "").lower()

        if db_type not in ADAPTERS:
            supported = ", ".join(ADAPTERS.keys())
            raise ValueError(f"Unsupported database type: {db_type}. Supported types: {supported}")

        adapter_class = ADAPTERS[db_type]
        if adapter_class is OracleAdapter and uses_odbc(config):
            adapter_class = ODBCAdapter
        return adapter_class(config)

    @staticmethod
    def register(db_type: str, adapter_class: type[DatabaseAdapter]) -> None:
        """Register a new database adapter type.

        Args:
            db_type: Database type name (e.g., 'sqlite', 'oracle').
            adapter_class: Adapter class inheriting from DatabaseAdapter.

        Example:
            from my_adapters import SQLiteAdapter
            DatabaseFactory.register('sqlite', SQLiteAdapter)
        """
        ADAPTERS[db_type.lower()] = adapter_class

    @staticmethod
    def get_supported_types() -> list[str]:
        """Get list of supported database types.

        Returns:
            List of supported database type names.
        """
        return list(ADAPTERS.keys())
