"""FastMCPHost tests: registration works without code generation."""

import inspect

import pytest

from stargate.adapters.fastmcp_host import FastMCPHost, build_signature
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


PET_SCHEMA = {
    "type": "object",
    "properties": {
        "petId": {"type": "integer", "description": "[path] Pet id"},
        "status": {"type": "string", "enum": ["available", "sold"]},
    },
    "required": ["petId"],
}


def test_build_signature_required_and_optional():
    signature = build_signature(PET_SCHEMA)

    assert signature is not None
    params = signature.parameters
    assert list(params) == ["petId", "status"]
    assert params["petId"].default is inspect.Parameter.empty
    assert params["status"].default is None
    assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())


def test_build_signature_rejects_unmappable_names():
    assert build_signature({"properties": {"_private": {}}}) is None
    assert build_signature({"properties": {"not-an-identifier": {}}}) is None
    assert build_signature({"properties": {"class": {}}}) is None


def test_tool_schema_is_published(host):
    async def handler(arguments):
        return arguments

    host.register_tool(MCPTool(name="getPet", input_schema=PET_SCHEMA), handler)

    published = host.mcp._tool_manager.get_tool("getPet").parameters
    assert set(published["properties"]) == {"petId", "status"}
    assert published["required"] == ["petId"]
    assert published["properties"]["petId"]["type"] == "integer"
    assert published["properties"]["petId"]["description"] == "[path] Pet id"
    assert published["properties"]["status"]["enum"] == ["available", "sold"]


async def test_tool_call_reaches_handler(host):
    received = []

    async def handler(arguments):
        received.append(arguments)
        return {"ok": True}

    host.register_tool(MCPTool(name="getPet", input_schema=PET_SCHEMA), handler)

    await host.mcp.call_tool("getPet", {"petId": 7})

    assert len(received) == 1
    assert received[0]["petId"] == 7
    assert received[0].get("status") is None


def test_tool_with_unmappable_property_still_registers(host):
    async def handler(arguments):
        return arguments

    schema = {"type": "object", "properties": {"_private": {"type": "string"}}}
    host.register_tool(MCPTool(name="odd", input_schema=schema), handler)

    assert host.mcp._tool_manager.get_tool("odd") is not None
