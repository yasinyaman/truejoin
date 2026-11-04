"""Old import paths keep working and emit a DeprecationWarning."""

import importlib
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"

# old module -> {exported name: new module | (new module, new name)}
SHIMS: dict[str, dict[str, str | tuple[str, str]]] = {
    "stargate.converter": {
        "convert_to_mcp": "stargate.bootstrap",
        "parse_spec": "stargate.bootstrap",
        "detect_format": "stargate.bootstrap",
        "PARSERS": "stargate.bootstrap",
    },
    "stargate.cli": {"main": "stargate.adapters.cli"},
    "stargate.models": {
        "MCPServer": "stargate.domain.mcp",
        "UnifiedAPISchema": "stargate.domain.api",
    },
    "stargate.models.unified": {
        "UnifiedAPISchema": "stargate.domain.api",
        "HTTPMethod": "stargate.domain.api",
    },
    "stargate.models.mcp": {
        "MCPTool": "stargate.domain.mcp",
        "MCPConversionResult": "stargate.domain.mcp",
    },
    "stargate.parsers": {
        "OpenAPIParser": "stargate.adapters.parsers.openapi",
        "BaseParser": "stargate.adapters.parsers.base",
    },
    "stargate.parsers.base": {"BaseParser": "stargate.adapters.parsers.base"},
    "stargate.parsers.openapi": {"OpenAPIParser": "stargate.adapters.parsers.openapi"},
    "stargate.parsers.postman": {"PostmanParser": "stargate.adapters.parsers.postman"},
    "stargate.parsers.manual": {
        "ManualParser": "stargate.adapters.parsers.manual",
        "create_manual_api": "stargate.adapters.parsers.manual",
    },
    "stargate.generators": {"MCPServerGenerator": "stargate.application.generators.server"},
    "stargate.generators.mcp_server": {"generate_server": "stargate.application.generators.server"},
    "stargate.generators.tools": {"ToolsGenerator": "stargate.application.generators.tools"},
    "stargate.generators.resources": {
        "ResourcesGenerator": "stargate.application.generators.resources"
    },
    "stargate.utils": {
        "validate_url": "stargate.domain.url_policy",
        "resolve_refs": "stargate.domain.schema",
        "HTTPClient": ("stargate.adapters.httpx_gateway", "HttpxApiGateway"),
    },
    "stargate.utils.http": {
        "HTTPClient": ("stargate.adapters.httpx_gateway", "HttpxApiGateway"),
        "fetch_spec_sync": "stargate.adapters.spec_loader",
    },
    "stargate.utils.schema": {"schema_to_mcp_input": "stargate.domain.schema"},
    "stargate.utils.security": {"validate_url": "stargate.domain.url_policy"},
    "stargate.server": {"MCPServerRuntime": "stargate.runtime"},
    "stargate.server.runtime": {"MCPServerRuntime": "stargate.runtime"},
}


@pytest.mark.parametrize("old_name", list(SHIMS), ids=list(SHIMS))
def test_shim_warns_and_reexports(old_name):
    with pytest.warns(DeprecationWarning, match=rf"{old_name} is deprecated"):
        module = importlib.reload(importlib.import_module(old_name))

    for name, target in SHIMS[old_name].items():
        target_module, target_name = target if isinstance(target, tuple) else (target, name)
        expected = getattr(importlib.import_module(target_module), target_name)
        assert getattr(module, name) is expected, f"{old_name}.{name}"


def test_deprecated_parse_file_still_works():
    from stargate.adapters.parsers.openapi import OpenAPIParser

    with pytest.warns(DeprecationWarning, match="parse_file is deprecated"):
        schema = OpenAPIParser().parse_file(FIXTURES / "openapi_petstore.json")

    assert schema.title == "Petstore API"
