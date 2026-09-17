"""FastMCPHost tests: registration works without code generation."""

import pytest

from stargate.adapters.fastmcp_host import FastMCPHost
from stargate.domain.mcp import MCPResource, MCPTool


@pytest.fixture
def host():
    return FastMCPHost("test")


async def test_template_resource_routes_uri_params(host):
    received = []

    async def handler(params):
        received.append(params)
        return "content-" + params["id"]

    host.register_resource(
        MCPResource(name="getProductById", uri="api://products/{id}", description="d"), handler
    )

    contents = await host.mcp.read_resource("api://products/42")

    assert received == [{"id": "42"}]
    assert [c.content for c in contents] == ["content-42"]


async def test_static_resource(host):
    received = []

    async def handler(params):
        received.append(params)
        return "all"

    host.register_resource(MCPResource(name="getAllProducts", uri="api://products"), handler)

    contents = await host.mcp.read_resource("api://products")

    assert received == [{}]
    assert [c.content for c in contents] == ["all"]


async def test_resource_metadata_is_published(host):
    async def handler(params):
        return ""

    host.register_resource(
        MCPResource(name="products", uri="api://products", description="All", mime_type="text/csv"),
        handler,
    )

    (resource,) = await host.mcp.list_resources()
    assert (resource.name, resource.description, resource.mimeType) == (
        "products",
        "All",
        "text/csv",
    )


def test_register_tool(host):
    async def handler(arguments):
        return arguments

    host.register_tool(MCPTool(name="listPets", description="List"), handler)

    tool = host.mcp._tool_manager.get_tool("listPets")
    assert tool is not None
    assert tool.description == "List"
