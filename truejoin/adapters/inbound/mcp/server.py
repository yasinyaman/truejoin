"""The curated MCP server: the OLAP tools, over stdio or streamable HTTP."""

from __future__ import annotations

import json
from collections.abc import Callable, Collection
from contextvars import ContextVar
from typing import Any

from truejoin.application.olap.app import FusionApp
from truejoin.application.olap.tool_schemas import TOOL_NAMES
from truejoin.domain.governance.access import UNRESTRICTED, AccessScope

#: Who the current HTTP request is, set by the ASGI host before the MCP app
#: runs. Unset means nobody was identified, which ``request_scope`` refuses.
CALLER: ContextVar[AccessScope | None] = ContextVar("truejoin_mcp_caller", default=None)


def request_scope() -> AccessScope:
    """The scope of the HTTP request being served.

    Raises:
        LookupError: When no host recorded one. Answering as an unrestricted
            caller instead would turn a wiring mistake into a data leak.
    """
    scope = CALLER.get()
    if scope is None:
        raise LookupError("No caller was identified for this MCP request")
    return scope


def _dump(result: dict[str, Any]) -> str:
    return json.dumps(result, default=str)


class _Scoped:
    """``execute`` on the tools, bound to whoever is calling right now."""

    def __init__(self, fusion: FusionApp, scope: Callable[[], AccessScope]) -> None:
        self._fusion = fusion
        self._scope = scope

    def execute(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        try:
            scope = self._scope()
        except LookupError as e:
            return {"error": str(e)}
        return self._fusion.tools.for_scope(scope).execute(name, arguments)


def create_mcp_server(
    fusion: FusionApp,
    name: str = "fusion",
    allowed: Collection[str] | None = None,
    scope: Callable[[], AccessScope] = lambda: UNRESTRICTED,
) -> Any:
    """Build an ``MCPServer`` whose tools delegate to ``fusion.tools``.

    Every tool returns JSON text; failures come back as ``{"error": ...}``
    so the model gets a uniform, parseable shape. ``allowed`` (see
    ``McpConfig.allowed_tools``) leaves only those tools on the server.
    ``scope`` says who each call runs as: one fixed caller over stdio,
    ``request_scope`` behind HTTP.
    """
    from mcp.server.mcpserver import MCPServer

    server = MCPServer(name)
    tools = _Scoped(fusion, scope)

    @server.tool()
    def list_sources() -> str:
        """List all connected data sources and their tables with row counts."""
        return _dump(tools.execute("list_sources"))

    @server.tool()
    def describe_table(table: str) -> str:
        """Show detailed schema for a table (columns, types, row count).

        Use 'source.table' format.
        """
        return _dump(tools.execute("describe_table", {"table": table}))

    @server.tool()
    def query_data(sql: str) -> str:
        """Execute an analytical SQL query on DuckDB. Only SELECT queries allowed. Max 100 rows."""
        return _dump(tools.execute("query_data", {"sql": sql}))

    @server.tool()
    def search_data(table: str, filter_column: str, filter_value: str, limit: int = 20) -> str:
        """Search rows in a table matching a filter.

        Supports exact match or LIKE with % wildcards.
        """
        return _dump(
            tools.execute(
                "search_data",
                {
                    "table": table,
                    "filter_column": filter_column,
                    "filter_value": filter_value,
                    "limit": limit,
                },
            )
        )

    @server.tool()
    def aggregate_data(table: str, group_by: str, agg_column: str, agg_func: str) -> str:
        """Run GROUP BY aggregation. agg_func: SUM, AVG, COUNT, MIN, or MAX."""
        return _dump(
            tools.execute(
                "aggregate_data",
                {
                    "table": table,
                    "group_by": group_by,
                    "agg_column": agg_column,
                    "agg_func": agg_func,
                },
            )
        )

    @server.tool()
    def create_view(name: str, sql: str, refresh: str = "manual") -> str:
        """Create a materialized view (cached aggregation). refresh: 'manual', 'hourly', 'daily'."""
        return _dump(tools.execute("create_view", {"name": name, "sql": sql, "refresh": refresh}))

    @server.tool()
    def list_views() -> str:
        """List all materialized views with refresh schedule and last refresh time."""
        return _dump(tools.execute("list_views"))

    @server.tool()
    def refresh_view(name: str) -> str:
        """Manually refresh a materialized view to get latest data."""
        return _dump(tools.execute("refresh_view", {"name": name}))

    @server.tool()
    def load_table(table: str, where: str | None = None, columns: list[str] | None = None) -> str:
        """Load a table, or only the rows and columns ``where``/``columns`` describe."""
        return _dump(
            tools.execute("load_table", {"table": table, "where": where, "columns": columns})
        )

    @server.tool()
    def list_metrics(table: str | None = None) -> str:
        """Measures, dimensions, grains and transforms available for a table."""
        return _dump(tools.execute("list_metrics", {"table": table}))

    @server.tool()
    def query_metrics(
        table: str,
        metrics: list[str],
        dimensions: list[str] | None = None,
        filters: list[dict] | None = None,
        order_by: str = "",
        limit: int = 100,
    ) -> str:
        """Answer a metric question without writing SQL (see list_metrics first)."""
        return _dump(
            tools.execute(
                "query_metrics",
                {
                    "table": table,
                    "metrics": metrics,
                    "dimensions": dimensions,
                    "filters": filters,
                    "order_by": order_by,
                    "limit": limit,
                },
            )
        )

    @server.tool()
    def cache_stats() -> str:
        """Show query cache statistics: hit rate, entry count, memory usage."""
        return _dump(tools.execute("cache_stats"))

    @server.tool()
    def check_readiness() -> str:
        """Check that this server can answer questions, and what is missing if not."""
        return _dump(tools.execute("check_readiness"))

    @server.tool()
    def find_examples(question: str = "", limit: int = 5) -> str:
        """Saved analyses close to a question: certified questions and the SQL that answers them."""
        return _dump(tools.execute("find_examples", {"question": question, "limit": limit}))

    @server.tool()
    def search_terms(query: str) -> str:
        """Find the certified business terms a word belongs to."""
        return _dump(tools.execute("search_terms", {"query": query}))

    @server.tool()
    def get_term(name: str) -> str:
        """Show one certified business term: tables, words, measures, reachable tables."""
        return _dump(tools.execute("get_term", {"name": name}))

    @server.tool()
    def check_question(question: str) -> str:
        """How much of a question the vocabulary covers, word by word; nothing runs."""
        return _dump(tools.execute("check_question", {"question": question}))

    @server.tool()
    def answer_question(question: str, previous: dict[str, Any] | None = None) -> str:
        """Answer a question asked in words; returns the reading and the SQL with the rows.

        For a follow-up ("and by branch?"), pass the earlier answer's request as previous.
        """
        args: dict[str, Any] = {"question": question}
        if previous is not None:
            args["previous"] = previous
        return _dump(tools.execute("answer_question", args))

    if allowed is not None:
        for tool_name in set(TOOL_NAMES) - set(allowed):
            server.remove_tool(tool_name)
    return server
