"""Use cases: invoke an MCP tool / read an MCP resource against the REST API.

Pure orchestration: argument routing, URL building and safety checks live here;
the actual HTTP call goes through the ApiGateway port.
"""

import json
from dataclasses import dataclass, field
from typing import Any

import structlog

from truejoin.domain.mcp.api import ParameterLocation
from truejoin.domain.mcp.mcp import MCPResource, MCPServer, MCPTool
from truejoin.domain.mcp.url_policy import validate_url
from truejoin.ports.api_gateway import ApiGateway, ApiResponse

logger = structlog.get_logger()

BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})

_DESCRIPTION_HINTS = (
    ("[path]", ParameterLocation.PATH),
    ("[header]", ParameterLocation.HEADER),
    ("[query]", ParameterLocation.QUERY),
)


@dataclass(frozen=True)
class PreparedRequest:
    """Everything the gateway needs to perform one call."""

    method: str
    url: str
    params: dict[str, Any] = field(default_factory=dict)
    json: dict[str, Any] | None = None
    headers: dict[str, str] = field(default_factory=dict)


def _location_from_description(prop: dict[str, Any]) -> ParameterLocation | None:
    """Legacy hint: older server.json files carry '[path]'/'[query]'/'[header]' in descriptions."""
    description = str(prop.get("description", "")).lower()
    for token, location in _DESCRIPTION_HINTS:
        if token in description:
            return location
    return None


def separate_params(
    tool: MCPTool,
    arguments: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str], dict[str, Any]]:
    """Splits tool arguments into (path, query, header, body) buckets.

    Resolution order per argument: MCPTool.parameter_locations, then the
    legacy description hint, then `{name}` present in the path template,
    then body for POST/PUT/PATCH, otherwise query. Cookie parameters are
    dropped. None values are skipped.
    """
    path_params: dict[str, Any] = {}
    query_params: dict[str, Any] = {}
    header_params: dict[str, str] = {}
    body_params: dict[str, Any] = {}

    properties = tool.input_schema.get("properties", {})

    for name, value in arguments.items():
        if value is None:
            continue

        location = tool.parameter_locations.get(name)
        if location is None:
            location = _location_from_description(properties.get(name, {}))
        if location is None and f"{{{name}}}" in tool.http_path:
            location = ParameterLocation.PATH

        if location == ParameterLocation.PATH:
            path_params[name] = value
        elif location == ParameterLocation.HEADER:
            header_params[name] = str(value)
        elif location == ParameterLocation.QUERY:
            query_params[name] = value
        elif location == ParameterLocation.COOKIE:
            logger.warning(
                "Cookie parameters are not supported; dropped", tool=tool.name, param=name
            )
        elif tool.http_method.upper() in BODY_METHODS:
            body_params[name] = value
        else:
            query_params[name] = value

    return path_params, query_params, header_params, body_params


def build_url(base_url: str, path: str) -> str:
    """Joins base URL and path with exactly one slash."""
    return f"{base_url.rstrip('/')}/{path.lstrip('/')}"


def build_tool_request(
    tool: MCPTool,
    arguments: dict[str, Any],
    server_base_url: str,
) -> PreparedRequest:
    """Turns tool arguments into a concrete HTTP request (not yet validated).

    Raises:
        ValueError: no base URL available for the tool
    """
    base_url = tool.base_url or server_base_url
    if not base_url:
        raise ValueError(f"Base URL not defined for tool: {tool.name}")

    path_params, query_params, _header_params, body_params = separate_params(tool, arguments)

    path = tool.http_path
    for name, value in path_params.items():
        path = path.replace(f"{{{name}}}", str(value))

    # Header parameters are collected but not forwarded (parity with the previous
    # runtime); pass `headers=_header_params` here to enable them.
    return PreparedRequest(
        method=tool.http_method.upper(),
        url=build_url(base_url, path),
        params=query_params,
        json=body_params or None,
    )


def resource_http_path(resource: MCPResource) -> str:
    """HTTP path of an API resource, derived from the URI when not stored.

    Raises:
        ValueError: neither http_path nor an api:// URI is available
    """
    if resource.http_path:
        return resource.http_path
    if resource.uri.startswith("api://"):
        return "/" + resource.uri[len("api://") :]
    raise ValueError(f"HTTP path not defined for resource: {resource.uri}")


def response_to_result(response: ApiResponse) -> dict[str, Any]:
    """Shapes an HTTP response as the tool result dict (status_code, headers, data)."""
    result: dict[str, Any] = {
        "status_code": response.status_code,
        "headers": dict(response.headers),
    }
    if "application/json" in response.content_type:
        try:
            result["data"] = response.json()
        except json.JSONDecodeError:
            result["data"] = response.text
    else:
        result["data"] = response.text
    return result


def _ensure_safe(url: str, allowed_domains: set[str] | None) -> None:
    """Refuse a URL the caller may not reach, before anything is sent.

    The gateway applies the same policy again, to every redirect hop it
    follows — a hop this layer cannot see. The duplication is deliberate: this
    check is what guarantees *no request is attempted at all*, which an
    adapter-side check cannot promise, and `ApiGateway` is a Protocol that does
    not oblige an implementation to check anything.
    """
    is_valid, error = validate_url(url, allowed_domains)
    if not is_valid:
        raise ValueError(f"Unsafe URL: {error}")


class ToolInvoker:
    """Executes MCP tool calls as HTTP requests."""

    def __init__(
        self,
        server: MCPServer,
        gateway: ApiGateway,
        allowed_domains: set[str] | None = None,
    ):
        self.server = server
        self.gateway = gateway
        self.allowed_domains = allowed_domains

    async def invoke(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Calls a tool by name.

        Raises:
            ValueError: unknown tool, missing base URL or unsafe URL
        """
        tool = self.server.get_tool(tool_name)
        if not tool:
            raise ValueError(f"Tool not found: {tool_name}")

        request = build_tool_request(tool, arguments, self.server.base_url)
        _ensure_safe(request.url, self.allowed_domains)

        logger.info("Executing tool", tool=tool.name, method=request.method, url=request.url)

        response = await self.gateway.request(
            request.method,
            request.url,
            params=request.params,
            json=request.json,
            headers=request.headers or None,
        )
        return response_to_result(response)


class ResourceReader:
    """Reads MCP resources: embedded schemas directly, API resources over HTTP."""

    def __init__(
        self,
        server: MCPServer,
        gateway: ApiGateway,
        allowed_domains: set[str] | None = None,
    ):
        self.server = server
        self.gateway = gateway
        self.allowed_domains = allowed_domains

    async def read(self, uri: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Reads a resource by (template) URI; `params` fill `{name}` placeholders.

        Returns:
            {"uri", "mimeType", "content"}

        Raises:
            ValueError: unknown resource, missing path/base URL or unsafe URL
        """
        resource = self.server.get_resource(uri)
        if not resource:
            raise ValueError(f"Resource not found: {uri}")

        if resource.schema_def:
            return {
                "uri": resource.uri,
                "mimeType": resource.mime_type,
                "content": json.dumps(resource.schema_def, indent=2),
            }

        base_url = resource.base_url or self.server.base_url
        if not base_url:
            raise ValueError(f"Base URL not defined for resource: {resource.uri}")

        path = resource_http_path(resource)
        for name, value in (params or {}).items():
            path = path.replace(f"{{{name}}}", str(value))

        url = build_url(base_url, path)
        _ensure_safe(url, self.allowed_domains)

        logger.info("Reading resource", uri=resource.uri, url=url)

        response = await self.gateway.request("GET", url)
        return {
            "uri": resource.uri,
            "mimeType": resource.mime_type,
            "content": response.text,
        }
