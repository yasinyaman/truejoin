"""MCPSdkHost tests: registration works without code generation (mcp 1.x and 2.x)."""

import inspect

import pytest

from stargate.adapters.mcp_sdk_host import MCPSdkHost, build_signature
from stargate.domain.mcp import MCPResource, MCPTool

PET_SCHEMA = {
    "type": "object",
    "properties": {
        "petId": {"type": "integer", "description": "[path] Pet id"},
        "status": {"type": "string", "enum": ["available", "sold"]},
    },
    "required": ["petId"],
}


def _attr(obj, snake: str, camel: str):
    """mcp 2.x exposes snake_case attributes, 1.x camelCase."""
    value = getattr(obj, snake, None)
    return value if value is not None else getattr(obj, camel)


def _contents(result) -> list:
    """Normalizes read_resource() results across SDK majors."""
    if hasattr(result, "contents"):
        items = result.contents
    elif hasattr(result, "content") and not isinstance(result, list | tuple):
        items = [result]
    else:
        items = list(result)
    return [getattr(item, "content", item) for item in items]


async def _tool(host: MCPSdkHost, name: str):
    return next(t for t in await host.mcp.list_tools() if t.name == name)


@pytest.fixture
def host():
    return MCPSdkHost("test")


async def test_template_resource_routes_uri_params(host):
    received = []

    async def handler(params):
        received.append(params)
        return "content-" + params["id"]

    host.register_resource(
        MCPResource(name="getProductById", uri="api://products/{id}", description="d"), handler
    )

    contents = _contents(await host.mcp.read_resource("api://products/42"))

    assert received == [{"id": "42"}]
    assert contents == ["content-42"]


async def test_static_resource(host):
    received = []

    async def handler(params):
        received.append(params)
        return "all"

    host.register_resource(MCPResource(name="getAllProducts", uri="api://products"), handler)

    contents = _contents(await host.mcp.read_resource("api://products"))

    assert received == [{}]
    assert contents == ["all"]


async def test_resource_metadata_is_published(host):
    async def handler(params):
        return ""

    host.register_resource(
        MCPResource(name="products", uri="api://products", description="All", mime_type="text/csv"),
        handler,
    )

    (resource,) = await host.mcp.list_resources()
    assert resource.name == "products"
    assert resource.description == "All"
    assert _attr(resource, "mime_type", "mimeType") == "text/csv"


async def test_register_tool(host):
    async def handler(arguments):
        return arguments

    host.register_tool(MCPTool(name="listPets", description="List"), handler)

    tool = await _tool(host, "listPets")
    assert tool.description == "List"


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


async def test_tool_schema_is_published(host):
    async def handler(arguments):
        return arguments

    host.register_tool(MCPTool(name="getPet", input_schema=PET_SCHEMA), handler)

    published = _attr(await _tool(host, "getPet"), "input_schema", "inputSchema")
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


async def test_tool_with_unmappable_property_still_registers(host):
    async def handler(arguments):
        return arguments

    schema = {"type": "object", "properties": {"_private": {"type": "string"}}}
    host.register_tool(MCPTool(name="odd", input_schema=schema), handler)

    assert await _tool(host, "odd") is not None
