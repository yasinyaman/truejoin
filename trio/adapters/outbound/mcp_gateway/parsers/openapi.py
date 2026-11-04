"""OpenAPI/Swagger 3.x parser."""

import re
from typing import Any

import structlog

from stargate.adapters.parsers.base import BaseParser
from stargate.domain.api import (
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
from stargate.domain.schema import resolve_refs

logger = structlog.get_logger()


class OpenAPIParser(BaseParser):
    """Parses OpenAPI 3.x specifications."""

    format_name = "openapi"

    def can_parse(self, spec: dict[str, Any]) -> bool:
        """Checks for OpenAPI 3.x format."""
        # OpenAPI 3.x
        if "openapi" in spec:
            version = spec.get("openapi", "")
            return version.startswith("3.")

        # Swagger 2.0 (limited support)
        if "swagger" in spec:
            version = spec.get("swagger", "")
            return version.startswith("2.")

        return False

    def parse(self, spec: dict[str, Any]) -> UnifiedAPISchema:
        """Parses OpenAPI specification."""
        self.clear_messages()

        # Version check
        openapi_version = spec.get("openapi", spec.get("swagger", ""))
        logger.info("Parsing OpenAPI", version=openapi_version)

        # Basic info
        info = spec.get("info", {})
        title = info.get("title", "Untitled API")
        version = info.get("version", "1.0.0")
        description = info.get("description", "")

        # Servers
        servers = self._parse_servers(spec)

        # Security schemes
        security_schemes = self._parse_security_schemes(spec)

        # Schema definitions (for references)
        definitions = self._get_definitions(spec)

        # Parse endpoints
        endpoints = self._parse_paths(spec, definitions)

        # Extract schemas
        schemas = definitions

        # Tag definitions
        tags = spec.get("tags", [])

        # Top-level x- extension fields (e.g. x-llm-context)
        extensions = {k: v for k, v in spec.items() if k.startswith("x-")}

        return UnifiedAPISchema(
            title=title,
            version=version,
            description=description,
            servers=servers,
            endpoints=endpoints,
            security_schemes=security_schemes,
            schemas=schemas,
            tags=tags,
            extensions=extensions,
        )

    def _parse_servers(self, spec: dict[str, Any]) -> list[ServerInfo]:
        """Parses server information."""
        servers = []

        # OpenAPI 3.x
        if "servers" in spec:
            for server in spec["servers"]:
                servers.append(
                    ServerInfo(
                        url=server.get("url", ""),
                        description=server.get("description", ""),
                        variables=server.get("variables", {}),
                    )
                )

        # Swagger 2.0
        elif "host" in spec:
            scheme = (spec.get("schemes", ["https"]) or ["https"])[0]
            host = spec.get("host", "")
            base_path = spec.get("basePath", "")
            url = f"{scheme}://{host}{base_path}"
            servers.append(ServerInfo(url=url))

        return servers

    def _parse_security_schemes(self, spec: dict[str, Any]) -> dict[str, SecurityScheme]:
        """Parses security schemes."""
        schemes = {}

        # OpenAPI 3.x
        components = spec.get("components", {})
        security_schemes = components.get("securitySchemes", {})

        # Swagger 2.0
        if not security_schemes:
            security_schemes = spec.get("securityDefinitions", {})

        for name, scheme_data in security_schemes.items():
            scheme_type = scheme_data.get("type", "")

            try:
                # Type mapping
                if scheme_type == "apiKey":
                    type_enum = SecuritySchemeType.API_KEY
                elif scheme_type == "http":
                    type_enum = SecuritySchemeType.HTTP
                elif scheme_type == "oauth2":
                    type_enum = SecuritySchemeType.OAUTH2
                elif scheme_type == "openIdConnect":
                    type_enum = SecuritySchemeType.OPENID_CONNECT
                else:
                    self.add_warning(f"Unknown security scheme type: {scheme_type}")
                    continue

                schemes[name] = SecurityScheme(
                    type=type_enum,
                    name=scheme_data.get("name", ""),
                    location=scheme_data.get("in", ""),
                    scheme=scheme_data.get("scheme", ""),
                    bearer_format=scheme_data.get("bearerFormat", ""),
                    description=scheme_data.get("description", ""),
                )
            except Exception as e:
                self.add_warning(f"Security scheme parse error ({name}): {e}")

        return schemes

    def _get_definitions(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Returns schema definitions."""
        # OpenAPI 3.x
        components = spec.get("components", {})
        schemas = components.get("schemas", {})

        # Swagger 2.0
        if not schemas:
            schemas = spec.get("definitions", {})

        return schemas

    def _parse_paths(
        self,
        spec: dict[str, Any],
        definitions: dict[str, Any],
    ) -> list[UnifiedEndpoint]:
        """Parses paths."""
        endpoints = []
        paths = spec.get("paths", {})

        for path, path_item in paths.items():
            if not isinstance(path_item, dict):
                continue

            # Path-level parameters
            path_params = path_item.get("parameters", [])

            for method in ("get", "post", "put", "delete", "patch", "head", "options"):
                if method not in path_item:
                    continue

                operation = path_item[method]
                if not isinstance(operation, dict):
                    continue

                try:
                    endpoint = self._parse_operation(
                        path=path,
                        method=method,
                        operation=operation,
                        path_params=path_params,
                        definitions=definitions,
                    )
                    endpoints.append(endpoint)
                except Exception as e:
                    self.add_error(f"Endpoint parse error ({method.upper()} {path}): {e}")

        return endpoints

    def _parse_operation(
        self,
        path: str,
        method: str,
        operation: dict[str, Any],
        path_params: list[dict[str, Any]],
        definitions: dict[str, Any],
    ) -> UnifiedEndpoint:
        """Parses a single operation."""
        # Operation ID
        operation_id = operation.get("operationId", "")
        if not operation_id:
            # Auto-generate
            clean_path = re.sub(r"[{}]", "", path)
            clean_path = re.sub(r"[^a-zA-Z0-9]", "_", clean_path)
            operation_id = f"{method}_{clean_path}".strip("_")

        # Descriptions
        summary = operation.get("summary", "")
        description = operation.get("description", "")

        # Parameters (path-level + operation-level)
        all_params = path_params + operation.get("parameters", [])
        parameters = self._parse_parameters(all_params, definitions)

        # Request body
        request_body = self._parse_request_body(operation, definitions)

        # Responses
        responses = self._parse_responses(operation, definitions)

        # Security
        security = self._parse_security(operation)

        # Tags
        tags = operation.get("tags", [])

        # Deprecated
        deprecated = operation.get("deprecated", False)

        # x- extension fields (e.g. x-llm-context)
        extensions = {k: v for k, v in operation.items() if k.startswith("x-")}

        return UnifiedEndpoint(
            path=path,
            method=HTTPMethod(method.upper()),
            operation_id=operation_id,
            summary=summary,
            description=description,
            parameters=parameters,
            request_body=request_body,
            responses=responses,
            security=security,
            tags=tags,
            deprecated=deprecated,
            extensions=extensions,
        )

    def _parse_parameters(
        self,
        params: list[dict[str, Any]],
        definitions: dict[str, Any],
    ) -> list[Parameter]:
        """Parses parameters."""
        parameters = []

        for param in params:
            if not isinstance(param, dict):
                continue

            # Resolve $ref
            if "$ref" in param:
                ref_path = param["$ref"].split("/")[-1]
                components = definitions.get("parameters", {})
                param = components.get(ref_path, param)

            name = param.get("name", "")
            if not name:
                continue

            location_str = param.get("in", "query")
            try:
                location = ParameterLocation(location_str)
            except ValueError:
                self.add_warning(f"Unknown parameter location: {location_str}")
                location = ParameterLocation.QUERY

            # Schema
            schema = param.get("schema", {"type": "string"})
            if definitions:
                schema = resolve_refs(schema, definitions)

            parameters.append(
                Parameter(
                    name=name,
                    location=location,
                    description=param.get("description", ""),
                    required=param.get("required", False),
                    schema=schema,
                    example=param.get("example"),
                )
            )

        return parameters

    def _parse_request_body(
        self,
        operation: dict[str, Any],
        definitions: dict[str, Any],
    ) -> RequestBody | None:
        """Parses request body."""
        # OpenAPI 3.x
        if "requestBody" in operation:
            rb = operation["requestBody"]
            content = {}

            for media_type, media_data in rb.get("content", {}).items():
                schema = media_data.get("schema", {})
                if definitions:
                    schema = resolve_refs(schema, definitions)

                content[media_type] = MediaType(
                    media_type=media_type,
                    schema=schema,
                    example=media_data.get("example"),
                )

            return RequestBody(
                description=rb.get("description", ""),
                required=rb.get("required", False),
                content=content,
            )

        # Swagger 2.0 - body parameter
        for param in operation.get("parameters", []):
            if param.get("in") == "body":
                schema = param.get("schema", {})
                if definitions:
                    schema = resolve_refs(schema, definitions)

                return RequestBody(
                    description=param.get("description", ""),
                    required=param.get("required", False),
                    content={
                        "application/json": MediaType(
                            media_type="application/json",
                            schema=schema,
                        )
                    },
                )

        return None

    def _parse_responses(
        self,
        operation: dict[str, Any],
        definitions: dict[str, Any],
    ) -> dict[str, Response]:
        """Parses responses."""
        responses = {}

        for status_code, response_data in operation.get("responses", {}).items():
            if not isinstance(response_data, dict):
                continue

            content = {}

            # OpenAPI 3.x
            if "content" in response_data:
                for media_type, media_data in response_data["content"].items():
                    schema = media_data.get("schema", {})
                    if definitions:
                        schema = resolve_refs(schema, definitions)

                    content[media_type] = MediaType(
                        media_type=media_type,
                        schema=schema,
                        example=media_data.get("example"),
                    )

            # Swagger 2.0
            elif "schema" in response_data:
                schema = response_data["schema"]
                if definitions:
                    schema = resolve_refs(schema, definitions)

                content["application/json"] = MediaType(
                    media_type="application/json",
                    schema=schema,
                )

            responses[str(status_code)] = Response(
                status_code=str(status_code),
                description=response_data.get("description", ""),
                content=content,
                headers=response_data.get("headers", {}),
            )

        return responses

    def _parse_security(self, operation: dict[str, Any]) -> list[SecurityRequirement]:
        """Parses security requirements."""
        security = []

        for sec_req in operation.get("security", []):
            if not isinstance(sec_req, dict):
                continue

            for scheme_name, scopes in sec_req.items():
                security.append(
                    SecurityRequirement(
                        scheme_name=scheme_name,
                        scopes=scopes if isinstance(scopes, list) else [],
                    )
                )

        return security
