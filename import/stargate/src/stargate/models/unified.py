"""Unified API Schema models.

All data from parsers is converted to these common models.
"""

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class HTTPMethod(str, Enum):
    """HTTP methods."""

    GET = "GET"
    POST = "POST"
    PUT = "PUT"
    DELETE = "DELETE"
    PATCH = "PATCH"
    HEAD = "HEAD"
    OPTIONS = "OPTIONS"


class ParameterLocation(str, Enum):
    """Parameter location."""

    QUERY = "query"
    PATH = "path"
    HEADER = "header"
    COOKIE = "cookie"


class SecuritySchemeType(str, Enum):
    """Security scheme type."""

    API_KEY = "apiKey"
    HTTP = "http"
    OAUTH2 = "oauth2"
    OPENID_CONNECT = "openIdConnect"


class Parameter(BaseModel):
    """API endpoint parameter."""

    name: str = Field(..., description="Parameter name")
    location: ParameterLocation = Field(..., description="Parameter location")
    description: str = Field(default="", description="Parameter description")
    required: bool = Field(default=False, description="Is required?")
    schema_: dict[str, Any] = Field(
        default_factory=lambda: {"type": "string"},
        alias="schema",
        description="JSON Schema definition",
    )
    example: Any = Field(default=None, description="Example value")

    model_config = {"populate_by_name": True}


class MediaType(BaseModel):
    """Content type definition."""

    media_type: str = Field(default="application/json", description="MIME type")
    schema_: dict[str, Any] = Field(
        default_factory=dict,
        alias="schema",
        description="JSON Schema definition",
    )
    example: Any = Field(default=None, description="Example value")

    model_config = {"populate_by_name": True}


class RequestBody(BaseModel):
    """HTTP request body."""

    description: str = Field(default="", description="Request body description")
    required: bool = Field(default=False, description="Is required?")
    content: dict[str, MediaType] = Field(
        default_factory=dict,
        description="Content types",
    )


class Response(BaseModel):
    """HTTP response definition."""

    status_code: str = Field(..., description="HTTP status code")
    description: str = Field(default="", description="Response description")
    content: dict[str, MediaType] = Field(
        default_factory=dict,
        description="Content types",
    )
    headers: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Response headers",
    )


class SecurityScheme(BaseModel):
    """Security scheme definition."""

    type: SecuritySchemeType = Field(..., description="Scheme type")
    name: str = Field(default="", description="Scheme name (for apiKey)")
    location: str = Field(default="", alias="in", description="Location (for apiKey)")
    scheme: str = Field(default="", description="HTTP scheme (bearer, basic)")
    bearer_format: str = Field(default="", alias="bearerFormat", description="Bearer format")
    description: str = Field(default="", description="Description")

    model_config = {"populate_by_name": True}


class SecurityRequirement(BaseModel):
    """Security requirement."""

    scheme_name: str = Field(..., description="Security scheme name")
    scopes: list[str] = Field(default_factory=list, description="OAuth2 scopes")


class UnifiedEndpoint(BaseModel):
    """Unified API endpoint definition."""

    path: str = Field(..., description="Endpoint path")
    method: HTTPMethod = Field(..., description="HTTP method")
    operation_id: str = Field(..., description="Operation ID")
    summary: str = Field(default="", description="Short description")
    description: str = Field(default="", description="Detailed description")
    parameters: list[Parameter] = Field(default_factory=list, description="Parameters")
    request_body: RequestBody | None = Field(default=None, description="Request body")
    responses: dict[str, Response] = Field(default_factory=dict, description="Responses")
    security: list[SecurityRequirement] = Field(
        default_factory=list, description="Security requirements"
    )
    tags: list[str] = Field(default_factory=list, description="Tags")
    deprecated: bool = Field(default=False, description="Is deprecated?")
    extensions: dict[str, Any] = Field(
        default_factory=dict,
        description="OpenAPI x- extension fields (e.g. x-llm-context)",
    )

    def get_tool_name(self) -> str:
        """Returns the MCP tool name."""
        return self.operation_id or f"{self.method.value.lower()}_{self.path.replace('/', '_')}"


class ServerInfo(BaseModel):
    """API server information."""

    url: str = Field(..., description="Server URL")
    description: str = Field(default="", description="Server description")
    variables: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="URL variables",
    )


class UnifiedAPISchema(BaseModel):
    """Unified API schema.

    All parsers produce this model.
    """

    title: str = Field(..., description="API title")
    version: str = Field(default="1.0.0", description="API version")
    description: str = Field(default="", description="API description")
    servers: list[ServerInfo] = Field(default_factory=list, description="Server list")
    endpoints: list[UnifiedEndpoint] = Field(default_factory=list, description="Endpoint list")
    security_schemes: dict[str, SecurityScheme] = Field(
        default_factory=dict,
        description="Security schemes",
    )
    schemas: dict[str, dict[str, Any]] = Field(
        default_factory=dict,
        description="Data schemas (components/schemas)",
    )
    tags: list[dict[str, Any]] = Field(default_factory=list, description="Tag definitions")
    extensions: dict[str, Any] = Field(
        default_factory=dict,
        description="Top-level OpenAPI x- extension fields (e.g. x-llm-context)",
    )

    def get_base_url(self) -> str:
        """Returns the first server URL."""
        if self.servers:
            return self.servers[0].url.rstrip("/")
        return ""

    def get_endpoints_by_tag(self, tag: str) -> list[UnifiedEndpoint]:
        """Returns endpoints with a specific tag."""
        return [ep for ep in self.endpoints if tag in ep.tags]

    def get_endpoints_by_method(self, method: HTTPMethod) -> list[UnifiedEndpoint]:
        """Returns endpoints with a specific HTTP method."""
        return [ep for ep in self.endpoints if ep.method == method]
