"""ToolInvoker / ResourceReader tests with a fake ApiGateway (no network)."""

import json
from typing import Any

import pytest

from truejoin.application.mcpgen.invoke import (
    ResourceReader,
    ToolInvoker,
    build_tool_request,
    resource_http_path,
    response_to_result,
    separate_params,
)
from truejoin.domain.mcp.api import ParameterLocation
from truejoin.domain.mcp.mcp import MCPResource, MCPServer, MCPTool
from truejoin.ports.api_gateway import ApiResponse

JSON_OK = ApiResponse(200, {"Content-Type": "application/json"}, '{"ok": true}')


class FakeGateway:
    def __init__(self, response: ApiResponse = JSON_OK):
        self.response = response
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    async def request(self, method, url, *, params=None, json=None, headers=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": dict(params or {}),
                "json": json,
                "headers": dict(headers or {}),
            }
        )
        return self.response

    async def aclose(self):
        self.closed = True


def make_tool(**overrides) -> MCPTool:
    base = {
        "name": "getItem",
        "http_method": "GET",
        "http_path": "/items/{id}",
        "input_schema": {"type": "object", "properties": {"id": {}, "q": {}}},
    }
    base.update(overrides)
    return MCPTool(**base)


def make_server(*tools: MCPTool, resources: list[MCPResource] | None = None) -> MCPServer:
    return MCPServer(
        name="Test",
        base_url="https://api.example.com",
        tools=list(tools),
        resources=resources or [],
    )


class TestSeparateParams:
    def test_structured_locations_win(self):
        tool = make_tool(
            parameter_locations={
                "id": ParameterLocation.PATH,
                "q": ParameterLocation.QUERY,
                "X-Trace": ParameterLocation.HEADER,
            }
        )

        path, query, header, body = separate_params(
            tool, {"id": 1, "q": "x", "X-Trace": 7, "extra": 2}
        )

        assert path == {"id": 1}
        assert query == {"q": "x", "extra": 2}
        assert header == {"X-Trace": "7"}
        assert body == {}

    def test_legacy_description_hints(self):
        tool = make_tool(
            http_path="/items",
            input_schema={
                "type": "object",
                "properties": {
                    "id": {"description": "[path]"},
                    "limit": {"description": "[query] max items"},
                    "X-H": {"description": "[header]"},
                },
            },
        )

        path, query, header, body = separate_params(tool, {"id": 1, "limit": 5, "X-H": "v"})

        assert path == {"id": 1}
        assert query == {"limit": 5}
        assert header == {"X-H": "v"}
        assert body == {}

    def test_path_template_inference(self):
        tool = make_tool(input_schema={"type": "object", "properties": {}})

        path, query, _, _ = separate_params(tool, {"id": 9, "q": "s"})

        assert path == {"id": 9}
        assert query == {"q": "s"}

    def test_body_for_write_methods(self):
        tool = make_tool(http_method="POST", http_path="/items")

        _, query, _, body = separate_params(tool, {"title": "a"})

        assert body == {"title": "a"}
        assert query == {}

    def test_none_values_skipped_and_cookies_dropped(self):
        tool = make_tool(parameter_locations={"session": ParameterLocation.COOKIE})

        path, query, header, body = separate_params(tool, {"q": None, "session": "abc"})

        assert (path, query, header, body) == ({}, {}, {}, {})


class TestBuildToolRequest:
    def test_substitutes_path_and_uses_server_base_url(self):
        tool = make_tool(parameter_locations={"id": ParameterLocation.PATH})

        request = build_tool_request(tool, {"id": 42, "q": "x"}, "https://api.example.com/")

        assert request.method == "GET"
        assert request.url == "https://api.example.com/items/42"
        assert request.params == {"q": "x"}
        assert request.json is None

    def test_tool_base_url_wins(self):
        tool = make_tool(base_url="https://tool.example.com", http_path="/x")

        assert build_tool_request(tool, {}, "https://server").url == "https://tool.example.com/x"

    def test_missing_base_url(self):
        with pytest.raises(ValueError, match="Base URL not defined"):
            build_tool_request(make_tool(), {}, "")


class TestResponseToResult:
    def test_json_body(self):
        assert response_to_result(JSON_OK) == {
            "status_code": 200,
            "headers": {"Content-Type": "application/json"},
            "data": {"ok": True},
        }

    def test_invalid_json_falls_back_to_text(self):
        response = ApiResponse(200, {"content-type": "application/json"}, "not json")
        assert response_to_result(response)["data"] == "not json"

    def test_text_body(self):
        response = ApiResponse(404, {"content-type": "text/plain"}, "nope")
        assert response_to_result(response)["data"] == "nope"


class TestToolInvoker:
    async def test_invoke_routes_through_gateway(self):
        gateway = FakeGateway()
        tool = make_tool(parameter_locations={"id": ParameterLocation.PATH})
        invoker = ToolInvoker(make_server(tool), gateway)

        result = await invoker.invoke("getItem", {"id": 1, "q": "x"})

        assert gateway.calls == [
            {
                "method": "GET",
                "url": "https://api.example.com/items/1",
                "params": {"q": "x"},
                "json": None,
                "headers": {},
            }
        ]
        assert result["status_code"] == 200
        assert result["data"] == {"ok": True}

    async def test_post_sends_json_body(self):
        gateway = FakeGateway()
        tool = make_tool(name="create", http_method="POST", http_path="/items")
        await ToolInvoker(make_server(tool), gateway).invoke("create", {"title": "t"})

        assert gateway.calls[0]["method"] == "POST"
        assert gateway.calls[0]["json"] == {"title": "t"}

    async def test_unknown_tool(self):
        with pytest.raises(ValueError, match="Tool not found"):
            await ToolInvoker(make_server(), FakeGateway()).invoke("nope", {})

    async def test_allowed_domains_enforced_before_request(self):
        gateway = FakeGateway()
        invoker = ToolInvoker(make_server(make_tool()), gateway, allowed_domains={"other.com"})

        with pytest.raises(ValueError, match="Unsafe URL"):
            await invoker.invoke("getItem", {"id": 1})
        assert gateway.calls == []

    async def test_private_hosts_rejected(self):
        gateway = FakeGateway()
        server = MCPServer(name="T", base_url="http://127.0.0.1", tools=[make_tool()])

        with pytest.raises(ValueError, match="Unsafe URL"):
            await ToolInvoker(server, gateway).invoke("getItem", {"id": 1})


class TestResourceReader:
    def make_reader(self, *resources: MCPResource, gateway: FakeGateway | None = None):
        gateway = gateway or FakeGateway(ApiResponse(200, {}, "body"))
        return ResourceReader(make_server(resources=list(resources)), gateway), gateway

    async def test_schema_resource_served_inline(self):
        resource = MCPResource(name="Pet", uri="schema://Pet", schema_def={"type": "object"})
        reader, gateway = self.make_reader(resource)

        result = await reader.read("schema://Pet")

        assert json.loads(result["content"]) == {"type": "object"}
        assert gateway.calls == []

    async def test_api_resource_path_derived_from_uri(self):
        reader, gateway = self.make_reader(MCPResource(name="products", uri="api://products"))

        result = await reader.read("api://products")

        assert gateway.calls[0]["url"] == "https://api.example.com/products"
        assert result == {
            "uri": "api://products",
            "mimeType": "application/json",
            "content": "body",
        }

    async def test_template_params_substituted(self):
        resource = MCPResource(
            name="product", uri="api://products/{id}", http_path="/products/{id}"
        )
        reader, gateway = self.make_reader(resource)

        await reader.read("api://products/{id}", {"id": "42"})

        assert gateway.calls[0]["url"] == "https://api.example.com/products/42"

    async def test_unknown_resource(self):
        reader, _ = self.make_reader()
        with pytest.raises(ValueError, match="Resource not found"):
            await reader.read("api://nope")

    async def test_missing_base_url(self):
        server = MCPServer(name="T", resources=[MCPResource(name="r", uri="api://r")])
        with pytest.raises(ValueError, match="Base URL not defined"):
            await ResourceReader(server, FakeGateway()).read("api://r")

    def test_resource_http_path_requires_api_uri(self):
        with pytest.raises(ValueError, match="HTTP path not defined"):
            resource_http_path(MCPResource(name="r", uri="custom://r"))
