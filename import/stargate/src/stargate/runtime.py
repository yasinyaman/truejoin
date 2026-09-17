"""MCP Server runtime - Runnable MCP server."""

import json
import re
from typing import Any

import httpx
import structlog

from stargate.adapters.mcp_wire import resource_to_mcp, tool_to_mcp
from stargate.domain.mcp import MCPResource, MCPServer, MCPTool
from stargate.domain.url_policy import validate_url

logger = structlog.get_logger()

# Regex pattern to find URI parameters like {id}, {userId}, etc.
URI_PARAM_PATTERN = re.compile(r"\{(\w+)\}")


class MCPServerRuntime:
    """Runnable MCP server.

    This class runs the created MCP Server as an actual MCP server.
    """

    def __init__(
        self,
        server: MCPServer,
        auth_token: str | None = None,
        auth_header: str = "Authorization",
        timeout: float = 30.0,
    ):
        """Creates MCPServerRuntime.

        Args:
            server: MCP Server definition
            auth_token: API authentication token
            auth_header: Auth header name
            timeout: HTTP request timeout
        """
        self.server = server
        self.auth_token = auth_token
        self.auth_header = auth_header
        self.timeout = timeout
        self._http_client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "MCPServerRuntime":
        """Context manager entry."""
        headers = {}
        if self.auth_token:
            if self.auth_token.lower().startswith("bearer "):
                headers[self.auth_header] = self.auth_token
            else:
                headers[self.auth_header] = f"Bearer {self.auth_token}"

        self._http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            headers=headers,
            follow_redirects=True,
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

    def get_tools(self) -> list[dict[str, Any]]:
        """Returns tool list in MCP format."""
        return [tool_to_mcp(tool) for tool in self.server.tools]

    def get_resources(self) -> list[dict[str, Any]]:
        """Returns resource list in MCP format."""
        return [resource_to_mcp(resource) for resource in self.server.resources]

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Calls MCP tool.

        Args:
            tool_name: Tool name
            arguments: Tool arguments

        Returns:
            Tool result

        Raises:
            ValueError: Tool not found or invalid arguments
        """
        tool = self.server.get_tool(tool_name)
        if not tool:
            raise ValueError(f"Tool not found: {tool_name}")

        return await self._execute_tool(tool, arguments)

    async def read_resource(self, uri: str) -> dict[str, Any]:
        """Reads MCP resource.

        Args:
            uri: Resource URI

        Returns:
            Resource content

        Raises:
            ValueError: Resource not found
        """
        resource = self.server.get_resource(uri)
        if not resource:
            raise ValueError(f"Resource not found: {uri}")

        return await self._fetch_resource(resource)

    async def _execute_tool(
        self,
        tool: MCPTool,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        """Executes tool as HTTP request."""
        if not self._http_client:
            raise RuntimeError("HTTP client not started. Use context manager.")

        # Get base_url - from tool or server
        base_url = tool.base_url or self.server.base_url
        if not base_url:
            raise ValueError(f"Base URL not defined for tool: {tool.name}")

        # Build URL
        url = self._build_url(base_url, tool.http_path, arguments)

        # URL security check
        is_valid, error = validate_url(url)
        if not is_valid:
            raise ValueError(f"Unsafe URL: {error}")

        # Separate parameters
        path_params, query_params, body_params = self._separate_params(tool, arguments)

        # Apply path parameters
        final_url = url
        for param_name, param_value in path_params.items():
            final_url = final_url.replace(f"{{{param_name}}}", str(param_value))

        logger.info(
            "Executing tool",
            tool=tool.name,
            method=tool.http_method,
            url=final_url,
        )

        # Send HTTP request
        method = tool.http_method.upper()

        if method == "GET":
            response = await self._http_client.get(final_url, params=query_params)
        elif method == "POST":
            response = await self._http_client.post(
                final_url, params=query_params, json=body_params or None
            )
        elif method == "PUT":
            response = await self._http_client.put(
                final_url, params=query_params, json=body_params or None
            )
        elif method == "PATCH":
            response = await self._http_client.patch(
                final_url, params=query_params, json=body_params or None
            )
        elif method == "DELETE":
            response = await self._http_client.delete(final_url, params=query_params)
        else:
            response = await self._http_client.request(
                method, final_url, params=query_params, json=body_params or None
            )

        # Process response
        return self._process_response(response)

    async def _fetch_resource(self, resource: MCPResource) -> dict[str, Any]:
        """Fetches resource via HTTP request."""
        # Schema resource
        if resource.schema_def:
            return {
                "uri": resource.uri,
                "mimeType": resource.mime_type,
                "content": json.dumps(resource.schema_def, indent=2),
            }

        # API resource
        # Get http_path - from resource or derive from URI
        http_path = resource.http_path
        if not http_path:
            # Derive from URI: api://products -> /products
            if resource.uri.startswith("api://"):
                http_path = "/" + resource.uri[6:]  # Remove "api://"
            else:
                raise ValueError(f"HTTP path not defined for resource: {resource.uri}")

        # Get base_url - from resource or server
        base_url = resource.base_url or self.server.base_url
        if not base_url:
            raise ValueError(f"Base URL not defined for resource: {resource.uri}")

        if not self._http_client:
            raise RuntimeError("HTTP client not started. Use context manager.")

        url = f"{base_url.rstrip('/')}/{http_path.lstrip('/')}"

        # URL security check
        is_valid, error = validate_url(url)
        if not is_valid:
            raise ValueError(f"Unsafe URL: {error}")

        logger.info("Reading resource", uri=resource.uri, url=url)

        response = await self._http_client.get(url)

        return {
            "uri": resource.uri,
            "mimeType": resource.mime_type,
            "content": response.text,
        }

    def _build_url(
        self,
        base_url: str,
        path: str,
        arguments: dict[str, Any],
    ) -> str:
        """Builds URL."""
        base = base_url.rstrip("/")
        path = path.lstrip("/")
        return f"{base}/{path}"

    def _separate_params(
        self,
        tool: MCPTool,
        arguments: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        """Separates parameters into path, query and body."""
        path_params = {}
        query_params = {}
        body_params = {}

        # Extract parameter types from input schema
        properties = tool.input_schema.get("properties", {})

        for arg_name, arg_value in arguments.items():
            if arg_value is None:
                continue

            prop = properties.get(arg_name, {})
            description = prop.get("description", "")

            # Extract location from description
            if "[path]" in description.lower():
                path_params[arg_name] = arg_value
            elif "[header]" in description.lower():
                # Headers are handled separately
                pass
            elif "[query]" in description.lower():
                query_params[arg_name] = arg_value
            elif "{" + arg_name + "}" in tool.http_path:
                # If in path, it's a path parameter
                path_params[arg_name] = arg_value
            elif tool.http_method.upper() in ("POST", "PUT", "PATCH"):
                # Body for POST/PUT/PATCH
                body_params[arg_name] = arg_value
            else:
                # Default: query
                query_params[arg_name] = arg_value

        return path_params, query_params, body_params

    def _process_response(self, response: httpx.Response) -> dict[str, Any]:
        """Processes HTTP response."""
        result = {
            "status_code": response.status_code,
            "headers": dict(response.headers),
        }

        content_type = response.headers.get("content-type", "")

        if "application/json" in content_type:
            try:
                result["data"] = response.json()
            except json.JSONDecodeError:
                result["data"] = response.text
        else:
            result["data"] = response.text

        return result

    def run(self, transport: str = "stdio"):
        """Runs MCP server.

        Args:
            transport: Transport type (stdio, sse, streamable-http)
        """
        from mcp.server.fastmcp import FastMCP

        # Create FastMCP instance
        mcp = FastMCP(self.server.name)

        # Register tools
        for tool in self.server.tools:
            self._register_tool(mcp, tool)

        # Register resources
        for resource in self.server.resources:
            self._register_resource(mcp, resource)

        logger.info(
            "Starting MCP server",
            name=self.server.name,
            transport=transport,
            tool_count=len(self.server.tools),
            resource_count=len(self.server.resources),
        )

        # Run server
        mcp.run(transport=transport)

    def _register_tool(self, mcp, tool: MCPTool):
        """Registers tool with MCP."""
        runtime = self

        async def tool_handler(**kwargs):
            async with runtime:
                return await runtime.call_tool(tool.name, kwargs)

        # Register tool
        mcp.tool(
            name=tool.name,
            description=tool.description,
        )(tool_handler)

    def _create_parameterized_resource_handler(
        self, resource: MCPResource, uri_params: list[str]
    ):
        """Creates a handler function with explicit parameters for FastMCP.
        
        FastMCP inspects function signatures, so we need explicit parameters
        instead of **kwargs for URI template parameters.
        """
        runtime = self
        res = resource
        params = uri_params
        
        # Build function code with explicit parameters
        param_str = ", ".join(params)
        func_code = f"""
async def resource_handler({param_str}):
    # Schema resources don't have HTTP paths
    if res.schema_def:
        return json.dumps(res.schema_def, indent=2)
    
    # Get http_path - from resource or derive from URI
    http_path = res.http_path
    if not http_path:
        # Derive from URI: api://products/{{id}} -> /products/{{id}}
        if res.uri.startswith("api://"):
            http_path = "/" + res.uri[6:]  # Remove "api://"
        else:
            raise ValueError(f"HTTP path not defined for resource: {{res.uri}}")
    
    # Get base_url - from resource or server
    base_url = res.base_url or runtime.server.base_url
    if not base_url:
        raise ValueError(f"Base URL not defined for resource: {{res.uri}}")
    
    # Build URL with parameters
    kwargs = {{{', '.join(f'"{p}": {p}' for p in params)}}}
    for param_name, param_value in kwargs.items():
        http_path = http_path.replace(f"{{{{{{param_name}}}}}}", str(param_value))
    
    url = f"{{base_url.rstrip('/')}}/{{http_path.lstrip('/')}}"
    
    # URL security check
    is_valid, error = validate_url(url)
    if not is_valid:
        raise ValueError(f"Unsafe URL: {{error}}")
    
    logger.info("Reading parameterized resource", uri=res.uri, url=url)
    
    async with runtime:
        response = await runtime._http_client.get(url)
        return response.text
"""
        
        # Execute to create the function
        local_vars = {
            "res": res,
            "runtime": runtime,
            "json": json,
            "validate_url": validate_url,
            "logger": logger,
        }
        exec(func_code, local_vars)
        return local_vars["resource_handler"]

    def _register_resource(self, mcp, resource: MCPResource):
        """Registers resource with MCP."""
        runtime = self
        res = resource  # Capture resource in closure
        
        # Extract URI parameters (e.g., {id} from api://products/{id})
        uri_params = URI_PARAM_PATTERN.findall(resource.uri)
        
        if uri_params:
            # Create handler with explicit parameters for FastMCP compatibility
            # FastMCP inspects function signature, so we need explicit params
            handler = self._create_parameterized_resource_handler(res, uri_params)
            
            # Register resource with parameters
            mcp.resource(resource.uri)(handler)
        else:
            # No parameters - simple handler
            async def resource_handler():
                async with runtime:
                    result = await runtime.read_resource(res.uri)
                    return result.get("content", "")

            # Register resource
            mcp.resource(resource.uri)(resource_handler)
