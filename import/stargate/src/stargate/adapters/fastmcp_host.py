"""Adapter: MCPHost implemented with FastMCP (mcp.server.fastmcp).

FastMCP derives tool/resource parameter schemas by inspecting function
signatures. Handlers here are generic ``**kwargs`` coroutines whose
``__signature__``/``__annotations__`` are set explicitly, so no code
generation is needed.
"""

import inspect
import keyword
import re
from typing import Annotated, Any

import structlog
from mcp.server.fastmcp import FastMCP
from pydantic import WithJsonSchema

from stargate.domain.mcp import MCPResource, MCPTool
from stargate.ports.mcp_host import ResourceHandler, ToolHandler

logger = structlog.get_logger()

# URI template parameters such as {id} in api://products/{id}
URI_PARAM_PATTERN = re.compile(r"\{(\w+)\}")

def build_signature(input_schema: dict[str, Any]) -> inspect.Signature | None:
    """Turns a JSON Schema object into keyword-only parameters for FastMCP.

    Each property keeps its original JSON Schema (description, enum, items, ...)
    via pydantic's WithJsonSchema; required properties have no default, the
    rest default to None.

    Returns:
        None when a property name cannot be a Python parameter (not an
        identifier, a keyword, or starting with '_', which FastMCP rejects).
    """
    properties = input_schema.get("properties") or {}
    required = set(input_schema.get("required") or [])

    parameters: list[inspect.Parameter] = []
    for name, prop in properties.items():
        if not name.isidentifier() or keyword.iskeyword(name) or name.startswith("_"):
            return None
        annotation = Annotated[Any, WithJsonSchema(prop if isinstance(prop, dict) else {})]
        default = inspect.Parameter.empty if name in required else None
        parameters.append(
            inspect.Parameter(
                name, inspect.Parameter.KEYWORD_ONLY, default=default, annotation=annotation
            )
        )
    return inspect.Signature(parameters)


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

        tool_fn.__name__ = re.sub(r"\W", "_", tool.name) or "tool"
        signature = build_signature(tool.input_schema)
        if signature is None:
            logger.warning(
                "Tool input schema cannot be expressed as a signature; exposing generic kwargs",
                tool=tool.name,
            )
        else:
            _set_signature(tool_fn, list(signature.parameters.values()))
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
