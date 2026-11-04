"""Manual API definition parser."""

import re
from typing import Any

import structlog

from trio.adapters.outbound.mcp_gateway.parsers.base import BaseParser
from trio.domain.mcp.api import (
    HTTPMethod,
    MediaType,
    Parameter,
    ParameterLocation,
    RequestBody,
    Response,
    SecurityRequirement,
    SecurityScheme,
    SecuritySchemeType,
    ServerInfo,
    UnifiedAPISchema,
    UnifiedEndpoint,
)

logger = structlog.get_logger()


class ManualEndpointConfig:
    """Manual endpoint configuration."""

    def __init__(
        self,
        path: str,
        method: str = "GET",
        name: str | None = None,
        description: str = "",
        parameters: list[dict[str, Any]] | None = None,
        request_body: dict[str, Any] | None = None,
        response_schema: dict[str, Any] | None = None,
        tags: list[str] | None = None,
        auth_required: bool = False,
    ):
        self.path = path
        self.method = method.upper()
        self.name = name
        self.description = description
        self.parameters = parameters or []
        self.request_body = request_body
        self.response_schema = response_schema
        self.tags = tags or []
        self.auth_required = auth_required


class ManualAPIConfig:
    """Manual API configuration."""

    def __init__(
        self,
        title: str,
        base_url: str,
        version: str = "1.0.0",
        description: str = "",
        endpoints: list[ManualEndpointConfig] | None = None,
        auth_type: str | None = None,
        auth_header: str = "Authorization",
    ):
        self.title = title
        self.base_url = base_url
        self.version = version
        self.description = description
        self.endpoints = endpoints or []
        self.auth_type = auth_type
        self.auth_header = auth_header


class ManualParser(BaseParser):
    """Parses manual API definitions."""

    format_name = "manual"

    def can_parse(self, spec: dict[str, Any]) -> bool:
        """Checks for manual format."""
        # Custom format: "stargate" or "manual" key
        if spec.get("format") in ("stargate", "manual"):
            return True

        # Simple format: base_url and endpoints
        if "base_url" in spec and "endpoints" in spec:
            return True

        return False

    def parse(self, spec: dict[str, Any]) -> UnifiedAPISchema:
        """Parses manual definition."""
        self.clear_messages()

        logger.info("Parsing manual API definition")

        # Basic info
        title = spec.get("title", spec.get("name", "Manual API"))
        version = spec.get("version", "1.0.0")
        description = spec.get("description", "")
        base_url = spec.get("base_url", spec.get("baseUrl", ""))

        # Server
        servers = []
        if base_url:
            servers.append(ServerInfo(url=base_url))

        # Auth
        security_schemes = self._parse_auth(spec)

        # Parse endpoints
        endpoints = self._parse_endpoints(spec.get("endpoints", []))

        # Tags
        tags = [{"name": t} for t in spec.get("tags", [])]

        return UnifiedAPISchema(
            title=title,
            version=version,
            description=description,
            servers=servers,
            endpoints=endpoints,
            security_schemes=security_schemes,
            schemas={},
            tags=tags,
        )

    def parse_config(self, config: ManualAPIConfig) -> UnifiedAPISchema:
        """Parses from ManualAPIConfig object."""
        self.clear_messages()

        servers = [ServerInfo(url=config.base_url)] if config.base_url else []

        security_schemes = {}
        if config.auth_type:
            if config.auth_type == "bearer":
                security_schemes["bearerAuth"] = SecurityScheme(
                    type=SecuritySchemeType.HTTP,
                    scheme="bearer",
                )
            elif config.auth_type == "apikey":
                security_schemes["apiKey"] = SecurityScheme(
                    type=SecuritySchemeType.API_KEY,
                    name=config.auth_header,
                    location="header",
                )
            elif config.auth_type == "basic":
                security_schemes["basicAuth"] = SecurityScheme(
                    type=SecuritySchemeType.HTTP,
                    scheme="basic",
                )

        endpoints = []
        for ep_config in config.endpoints:
            endpoint = self._create_endpoint(ep_config)
            endpoints.append(endpoint)

        return UnifiedAPISchema(
            title=config.title,
            version=config.version,
            description=config.description,
            servers=servers,
            endpoints=endpoints,
            security_schemes=security_schemes,
        )

    def _parse_auth(self, spec: dict[str, Any]) -> dict[str, SecurityScheme]:
        """Parses auth information."""
        schemes = {}

        auth = spec.get("auth", spec.get("authentication", {}))
        if not auth:
            return schemes

        auth_type = auth.get("type", "")

        if auth_type == "bearer":
            schemes["bearerAuth"] = SecurityScheme(
                type=SecuritySchemeType.HTTP,
                scheme="bearer",
                description=auth.get("description", "Bearer token authentication"),
            )

        elif auth_type == "apikey":
            schemes["apiKey"] = SecurityScheme(
                type=SecuritySchemeType.API_KEY,
                name=auth.get("name", auth.get("header", "X-API-Key")),
                location=auth.get("in", "header"),
                description=auth.get("description", "API Key authentication"),
            )

        elif auth_type == "basic":
            schemes["basicAuth"] = SecurityScheme(
                type=SecuritySchemeType.HTTP,
                scheme="basic",
                description=auth.get("description", "Basic authentication"),
            )

        return schemes

    def _parse_endpoints(self, endpoints_data: list[dict[str, Any]]) -> list[UnifiedEndpoint]:
        """Parses endpoint list."""
        endpoints = []

        for ep_data in endpoints_data:
            if not isinstance(ep_data, dict):
                continue

            try:
                endpoint = self._parse_endpoint(ep_data)
                endpoints.append(endpoint)
            except Exception as e:
                self.add_error(f"Endpoint parse error: {e}")

        return endpoints

    def _parse_endpoint(self, ep_data: dict[str, Any]) -> UnifiedEndpoint:
        """Parses a single endpoint."""
        # Path and method
        path = ep_data.get("path", ep_data.get("url", "/"))
        method_str = ep_data.get("method", "GET").upper()

        try:
            method = HTTPMethod(method_str)
        except ValueError:
            raise ValueError(f"Invalid HTTP method: {method_str}") from None

        # Name and description
        name = ep_data.get("name", ep_data.get("operationId", ""))
        description = ep_data.get("description", "")
        summary = ep_data.get("summary", name)

        # Operation ID
        operation_id = ep_data.get("operationId", "")
        if not operation_id:
            operation_id = self._generate_operation_id(name, method_str, path)

        # Parameters
        parameters = self._parse_parameters(ep_data.get("parameters", []), path)

        # Request body
        request_body = self._parse_request_body(ep_data)

        # Response
        responses = self._parse_responses(ep_data)

        # Security
        security = []
        if ep_data.get("auth", ep_data.get("auth_required", False)):
            security.append(SecurityRequirement(scheme_name="bearerAuth", scopes=[]))

        # Tags
        tags = ep_data.get("tags", [])
        if isinstance(tags, str):
            tags = [tags]

        return UnifiedEndpoint(
            path=path,
            method=method,
            operation_id=operation_id,
            summary=summary,
            description=description,
            parameters=parameters,
            request_body=request_body,
            responses=responses,
            security=security,
            tags=tags,
        )

    def _create_endpoint(self, config: ManualEndpointConfig) -> UnifiedEndpoint:
        """Creates endpoint from ManualEndpointConfig."""
        method = HTTPMethod(config.method)

        operation_id = config.name or self._generate_operation_id("", config.method, config.path)

        parameters = self._parse_parameters(config.parameters, config.path)

        request_body = None
        if config.request_body:
            request_body = RequestBody(
                required=True,
                content={
                    "application/json": MediaType(
                        media_type="application/json",
                        schema=config.request_body,
                    )
                },
            )

        responses = {}
        if config.response_schema:
            responses["200"] = Response(
                status_code="200",
                description="Successful response",
                content={
                    "application/json": MediaType(
                        media_type="application/json",
                        schema=config.response_schema,
                    )
                },
            )
        else:
            responses["200"] = Response(
                status_code="200",
                description="Successful response",
            )

        security = []
        if config.auth_required:
            security.append(SecurityRequirement(scheme_name="bearerAuth", scopes=[]))

        return UnifiedEndpoint(
            path=config.path,
            method=method,
            operation_id=operation_id,
            summary=config.name or "",
            description=config.description,
            parameters=parameters,
            request_body=request_body,
            responses=responses,
            security=security,
            tags=config.tags,
        )

    def _generate_operation_id(self, name: str, method: str, path: str) -> str:
        """Generates operation ID."""
        if name:
            op_id = re.sub(r"[^a-zA-Z0-9]", "_", name)
            op_id = re.sub(r"_+", "_", op_id)
            return op_id.strip("_").lower()

        clean_path = re.sub(r"[{}]", "", path)
        clean_path = re.sub(r"[^a-zA-Z0-9]", "_", clean_path)
        return f"{method.lower()}_{clean_path}".strip("_")

    def _parse_parameters(
        self,
        params_data: list[dict[str, Any]],
        path: str,
    ) -> list[Parameter]:
        """Parses parameters."""
        parameters = []

        # Extract parameters from path
        path_params = re.findall(r"\{(\w+)\}", path)
        for param_name in path_params:
            # Add default if not defined in params_data
            if not any(p.get("name") == param_name for p in params_data):
                parameters.append(
                    Parameter(
                        name=param_name,
                        location=ParameterLocation.PATH,
                        required=True,
                        schema={"type": "string"},
                    )
                )

        # Add defined parameters
        for param in params_data:
            if not isinstance(param, dict):
                continue

            name = param.get("name", "")
            if not name:
                continue

            location_str = param.get("in", param.get("location", "query"))
            try:
                location = ParameterLocation(location_str)
            except ValueError:
                location = ParameterLocation.QUERY

            schema = param.get("schema", {"type": param.get("type", "string")})

            parameters.append(
                Parameter(
                    name=name,
                    location=location,
                    description=param.get("description", ""),
                    required=param.get("required", location == ParameterLocation.PATH),
                    schema=schema,
                    example=param.get("example"),
                )
            )

        return parameters

    def _parse_request_body(self, ep_data: dict[str, Any]) -> RequestBody | None:
        """Parses request body."""
        body = ep_data.get("body", ep_data.get("requestBody", {}))

        if not body:
            return None

        if isinstance(body, dict):
            schema = body.get("schema", body)
            content_type = body.get("contentType", "application/json")

            return RequestBody(
                description=body.get("description", ""),
                required=body.get("required", True),
                content={
                    content_type: MediaType(
                        media_type=content_type,
                        schema=schema,
                        example=body.get("example"),
                    )
                },
            )

        return None

    def _parse_responses(self, ep_data: dict[str, Any]) -> dict[str, Response]:
        """Parses responses."""
        responses = {}

        response_data = ep_data.get("response", ep_data.get("responses", {}))

        if isinstance(response_data, dict):
            # Given as schema
            if "type" in response_data or "properties" in response_data:
                responses["200"] = Response(
                    status_code="200",
                    description="Successful response",
                    content={
                        "application/json": MediaType(
                            media_type="application/json",
                            schema=response_data,
                        )
                    },
                )
            else:
                # By status code
                for status, resp in response_data.items():
                    if isinstance(resp, dict):
                        schema = resp.get("schema", {})
                        responses[str(status)] = Response(
                            status_code=str(status),
                            description=resp.get("description", ""),
                            content={
                                "application/json": MediaType(
                                    media_type="application/json",
                                    schema=schema,
                                )
                            }
                            if schema
                            else {},
                        )

        # Default 200 response
        if not responses:
            responses["200"] = Response(
                status_code="200",
                description="Successful response",
            )

        return responses


def create_manual_api(
    title: str,
    base_url: str,
    endpoints: list[dict[str, Any]],
    **kwargs,
) -> UnifiedAPISchema:
    """Quick manual API creation helper.

    Args:
        title: API title
        base_url: API base URL
        endpoints: List of endpoint definitions
        **kwargs: Additional parameters (version, description, auth, etc.)

    Returns:
        UnifiedAPISchema

    Example:
        >>> api = create_manual_api(
        ...     title="My API",
        ...     base_url="https://api.example.com",
        ...     endpoints=[
        ...         {"path": "/users", "method": "GET", "name": "listUsers"},
        ...         {"path": "/users/{id}", "method": "GET", "name": "getUser"},
        ...     ]
        ... )
    """
    spec = {
        "format": "manual",
        "title": title,
        "base_url": base_url,
        "endpoints": endpoints,
        **kwargs,
    }

    parser = ManualParser()
    return parser.parse(spec)
