"""Adapter: MCPHost implemented with FastMCP (mcp.server.fastmcp).

FastMCP derives tool/resource parameter schemas by inspecting function
signatures. Handlers here are generic ``**kwargs`` coroutines whose
``__signature__``/``__annotations__`` are set explicitly, so no code
generation is needed.
"""

import inspect
import re
from typing import Any

import structlog
from mcp.server.fastmcp import FastMCP

from stargate.domain.mcp import MCPResource, MCPTool
from stargate.ports.mcp_host import ResourceHandler, ToolHandler

logger = structlog.get_logger()

# URI template parameters such as {id} in api://products/{id}
URI_PARAM_PATTERN = re.compile(r"\{(\w+)\}")

JSON_TYPE_TO_PY: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


def _set_signature(fn: Any, parameters: list[inspect.Parameter]) -> None:
    """Makes a **kwargs function look like it has explicit keyword-only parameters."""
    fn.__signature__ = inspect.Signature(parameters)
    fn.__annotations__ = {
        p.name: p.annotation for p in parameters if p.annotation is not inspect.Parameter.empty
    }


class FastMCPHost:
    """Publishes tools and resources on a FastMCP server."""

    def __init__(self, name: str):
        self._mcp = FastMCP(name)

    @property
    def mcp(self) -> FastMCP:
        """Underlying FastMCP instance (introspection / tests)."""
        return self._mcp

    def register_tool(self, tool: MCPTool, handler: ToolHandler) -> None:
        async def tool_fn(**kwargs: Any) -> Any:
            return await handler(kwargs)

        tool_fn.__name__ = tool.name
        self._mcp.tool(name=tool.name, description=tool.description)(tool_fn)

    def register_resource(self, resource: MCPResource, handler: ResourceHandler) -> None:
        uri_params = URI_PARAM_PATTERN.findall(resource.uri)

        if uri_params:

            async def template_fn(**kwargs: Any) -> str:
                return await handler({name: str(value) for name, value in kwargs.items()})

            _set_signature(
                template_fn,
                [
                    inspect.Parameter(name, inspect.Parameter.KEYWORD_ONLY, annotation=str)
                    for name in uri_params
                ],
            )
            fn: Any = template_fn
        else:

            async def resource_fn() -> str:
                return await handler({})

            fn = resource_fn

        fn.__name__ = resource.name
        self._mcp.resource(
            resource.uri,
            name=resource.name,
            description=resource.description,
            mime_type=resource.mime_type,
        )(fn)

    def run(self, transport: str = "stdio") -> None:
        self._mcp.run(transport=transport)
