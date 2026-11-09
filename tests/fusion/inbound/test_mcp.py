"""Tests for the MCP server adapter (mcp 2.x MCPServer)."""

import asyncio
import inspect
import json

import pytest

from trio.adapters.inbound.mcp.server import create_mcp_server
from trio.application.olap.tool_schemas import TOOL_NAMES


def _await(value):
    return asyncio.run(value) if inspect.isawaitable(value) else value


def _text(result):
    """Extract the text payload from a CallToolResult (or plain return)."""
    content = getattr(result, "content", result)
    if isinstance(content, list) and content:
        first = content[0]
        return getattr(first, "text", first)
    return content


@pytest.fixture
def server(app_with_data):
    return create_mcp_server(app_with_data)


class TestMCPServer:
    def test_registers_ten_tools(self, server):
        tools = _await(server.list_tools())
        assert [t.name for t in tools] == list(TOOL_NAMES)
        assert all(t.description for t in tools)

    def test_call_tool_returns_json_text(self, server):
        result = _await(server.call_tool("list_sources", {}))
        payload = json.loads(_text(result))
        assert payload["sources"][0]["source"] == "test_db"

    def test_call_query_tool(self, server):
        result = _await(
            server.call_tool("query_data", {"sql": "SELECT COUNT(*) AS c FROM test_db.users"})
        )
        assert json.loads(_text(result))["rows"] == [{"c": 5}]

    def test_errors_are_json_not_exceptions(self, server):
        result = _await(server.call_tool("query_data", {"sql": "DROP TABLE test_db.users"}))
        assert "error" in json.loads(_text(result))

    def test_search_defaults(self, server):
        result = _await(
            server.call_tool(
                "search_data",
                {"table": "test_db.users", "filter_column": "name", "filter_value": "Alice"},
            )
        )
        assert json.loads(_text(result))["row_count"] == 1


def test_load_table_accepts_a_slice(server):
    result = _await(
        server.call_tool(
            "load_table",
            {"table": "test_db.orders", "where": "product = 'A'", "columns": ["id", "product"]},
        )
    )
    payload = json.loads(_text(result))
    assert payload["status"] == "loaded"
    assert payload["slice"] == "columns=id,product where product = 'A'"
    assert payload["row_count"] == 3


class TestTermTools:
    def test_a_server_built_without_them_does_not_list_them(self, app_with_data):
        offered = set(TOOL_NAMES) - {"search_terms", "get_term", "answer_question"}
        server = create_mcp_server(app_with_data, allowed=offered)
        assert {t.name for t in _await(server.list_tools())} == offered

    def test_with_no_certified_terms_a_question_gets_the_way_forward(self, server):
        result = _await(server.call_tool("answer_question", {"question": "how many users"}))
        assert "trio catalog review" in json.loads(_text(result))["error"]
        follow_up = {"question": "and by segment?", "previous": {"table": "db.users"}}
        result = _await(server.call_tool("answer_question", follow_up))
        assert "trio catalog review" in json.loads(_text(result))["error"]
        result = _await(server.call_tool("search_terms", {"query": "users"}))
        assert json.loads(_text(result)) == {"terms": [], "count": 0}
        result = _await(server.call_tool("get_term", {"name": "users"}))
        assert "Certified terms: none" in json.loads(_text(result))["error"]
