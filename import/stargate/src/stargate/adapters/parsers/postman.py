"""Postman Collection v2.1 parser."""

import re
from typing import Any
from urllib.parse import urlparse

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

logger = structlog.get_logger()


class PostmanParser(BaseParser):
    """Parses Postman Collection v2.1 format."""

    format_name = "postman"

    def can_parse(self, spec: dict[str, Any]) -> bool:
        """Checks for Postman Collection format."""
        # Postman Collection v2.1
        if "info" in spec and "_postman_id" in spec.get("info", {}):
            return True

        # Schema URL check
        schema = spec.get("info", {}).get("schema", "")
        if "schema.getpostman.com" in schema:
            return True

        # Item array check (Postman-specific)
        if "item" in spec and isinstance(spec["item"], list):
            return True

        return False

    def parse(self, spec: dict[str, Any]) -> UnifiedAPISchema:
        """Parses Postman Collection."""
        self.clear_messages()

        logger.info("Parsing Postman Collection")

        # Basic info
        info = spec.get("info", {})
        title = info.get("name", "Untitled API")
        version = info.get("version", "1.0.0")
        description = info.get("description", "")

        # Variables (for base URL)
        variables = self._parse_variables(spec)
        base_url = variables.get("baseUrl", variables.get("base_url", ""))

        servers = []
        if base_url:
            servers.append(ServerInfo(url=base_url))

        # Extract auth info
        security_schemes = self._parse_auth(spec)

        # Parse endpoints
        endpoints = self._parse_items(spec.get("item", []), variables)

        # Extract tags (from folders)
        tags = self._extract_tags(spec.get("item", []))

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

    def _parse_variables(self, spec: dict[str, Any]) -> dict[str, str]:
        """Parses collection variables."""
        variables = {}

        for var in spec.get("variable", []):
            key = var.get("key", "")
            value = var.get("value", "")
            if key:
                variables[key] = value

        return variables

    def _parse_auth(self, spec: dict[str, Any]) -> dict[str, SecurityScheme]:
        """Parses auth information."""
        schemes = {}
        auth = spec.get("auth", {})

        if not auth:
            return schemes

        auth_type = auth.get("type", "")

        if auth_type == "apikey":
            api_key_data = auth.get("apikey", [])
            key_name = ""
            key_in = "header"

            for item in api_key_data:
                if item.get("key") == "key":
                    key_name = item.get("value", "")
                elif item.get("key") == "in":
                    key_in = item.get("value", "header")

            schemes["apiKey"] = SecurityScheme(
                type=SecuritySchemeType.API_KEY,
                name=key_name,
                location=key_in,
                description="API Key authentication",
            )

        elif auth_type == "bearer":
            schemes["bearerAuth"] = SecurityScheme(
                type=SecuritySchemeType.HTTP,
                scheme="bearer",
                description="Bearer token authentication",
            )

        elif auth_type == "basic":
            schemes["basicAuth"] = SecurityScheme(
                type=SecuritySchemeType.HTTP,
                scheme="basic",
                description="Basic authentication",
            )

        elif auth_type == "oauth2":
            schemes["oauth2"] = SecurityScheme(
                type=SecuritySchemeType.OAUTH2,
                description="OAuth 2.0 authentication",
            )

        return schemes

    def _parse_items(
        self,
        items: list[dict[str, Any]],
        variables: dict[str, str],
        parent_tags: list[str] | None = None,
    ) -> list[UnifiedEndpoint]:
        """Parses items recursively."""
        endpoints = []
        parent_tags = parent_tags or []

        for item in items:
            if not isinstance(item, dict):
                continue

            # Folder (nested items)
            if "item" in item:
                folder_name = item.get("name", "")
                folder_tags = parent_tags + [folder_name] if folder_name else parent_tags
                nested_endpoints = self._parse_items(item["item"], variables, folder_tags)
                endpoints.extend(nested_endpoints)
                continue

            # Request
            if "request" not in item:
                continue

            try:
                endpoint = self._parse_request(item, variables, parent_tags)
                if endpoint:
                    endpoints.append(endpoint)
            except Exception as e:
                self.add_error(f"Request parse error ({item.get('name', 'unknown')}): {e}")

        return endpoints

    def _parse_request(
        self,
        item: dict[str, Any],
        variables: dict[str, str],
        tags: list[str],
    ) -> UnifiedEndpoint | None:
        """Parses a single request."""
        request = item.get("request", {})

        if isinstance(request, str):
            # Simple URL format
            return None

        # Method
        method_str = request.get("method", "GET").upper()
        try:
            method = HTTPMethod(method_str)
        except ValueError:
            self.add_warning(f"Unknown HTTP method: {method_str}")
            return None

        # URL
        url_data = request.get("url", {})
        if isinstance(url_data, str):
            url_data = {"raw": url_data}

        path = self._extract_path(url_data, variables)
        if not path:
            return None

        # Operation ID
        name = item.get("name", "")
        operation_id = self._generate_operation_id(name, method_str, path)

        # Description
        description = request.get("description", "")
        if isinstance(description, dict):
            description = description.get("content", "")

        # Parameters
        parameters = self._parse_parameters(url_data, request)

        # Request body
        request_body = self._parse_body(request)

        # Response examples
        responses = self._parse_responses(item)

        # Security
        security = self._parse_request_auth(request)

        return UnifiedEndpoint(
            path=path,
            method=method,
            operation_id=operation_id,
            summary=name,
            description=description,
            parameters=parameters,
            request_body=request_body,
            responses=responses,
            security=security,
            tags=tags,
        )

    def _extract_path(
        self,
        url_data: dict[str, Any],
        variables: dict[str, str],
    ) -> str:
        """Extracts path from URL."""
        # From path array
        path_parts = url_data.get("path", [])
        if path_parts:
            path = "/" + "/".join(str(p) for p in path_parts)
        else:
            # From raw URL
            raw = url_data.get("raw", "")
            raw = self._replace_variables(raw, variables)

            try:
                parsed = urlparse(raw)
                path = parsed.path or "/"
            except Exception:
                path = "/"

        # Convert variables to path parameter format
        # {{id}} -> {id}
        path = re.sub(r"\{\{(\w+)\}\}", r"{\1}", path)
        # :id -> {id}
        path = re.sub(r":(\w+)", r"{\1}", path)

        return path

    def _replace_variables(self, text: str, variables: dict[str, str]) -> str:
        """Replaces {{variable}} format variables."""
        for key, value in variables.items():
            text = text.replace(f"{{{{{key}}}}}", value)
        return text

    def _generate_operation_id(self, name: str, method: str, path: str) -> str:
        """Generates operation ID."""
        if name:
            # Generate from name
            op_id = re.sub(r"[^a-zA-Z0-9]", "_", name)
            op_id = re.sub(r"_+", "_", op_id)
            return op_id.strip("_").lower()

        # Generate from path
        clean_path = re.sub(r"[{}]", "", path)
        clean_path = re.sub(r"[^a-zA-Z0-9]", "_", clean_path)
        return f"{method.lower()}_{clean_path}".strip("_")

    def _parse_parameters(
        self,
        url_data: dict[str, Any],
        request: dict[str, Any],
    ) -> list[Parameter]:
        """Parses parameters."""
        parameters = []

        # Path parameters
        path_vars = url_data.get("variable", [])
        for var in path_vars:
            if not isinstance(var, dict):
                continue

            parameters.append(
                Parameter(
                    name=var.get("key", ""),
                    location=ParameterLocation.PATH,
                    description=var.get("description", ""),
                    required=True,
                    schema={"type": "string"},
                    example=var.get("value"),
                )
            )

        # Query parameters
        query_params = url_data.get("query", [])
        for param in query_params:
            if not isinstance(param, dict):
                continue

            # Skip disabled parameters
            if param.get("disabled", False):
                continue

            parameters.append(
                Parameter(
                    name=param.get("key", ""),
                    location=ParameterLocation.QUERY,
                    description=param.get("description", ""),
                    required=False,
                    schema={"type": "string"},
                    example=param.get("value"),
                )
            )

        # Header parameters
        headers = request.get("header", [])
        for header in headers:
            if not isinstance(header, dict):
                continue

            if header.get("disabled", False):
                continue

            # Skip standard headers
            key = header.get("key", "").lower()
            if key in ("content-type", "accept", "authorization"):
                continue

            parameters.append(
                Parameter(
                    name=header.get("key", ""),
                    location=ParameterLocation.HEADER,
                    description=header.get("description", ""),
                    required=False,
                    schema={"type": "string"},
                    example=header.get("value"),
                )
            )

        return parameters

    def _parse_body(self, request: dict[str, Any]) -> RequestBody | None:
        """Parses request body."""
        body = request.get("body", {})
        if not body:
            return None

        mode = body.get("mode", "")
        content = {}

        if mode == "raw":
            raw_content = body.get("raw", "")
            options = body.get("options", {})
            language = options.get("raw", {}).get("language", "json")

            if language == "json":
                media_type = "application/json"
                schema = self._infer_schema_from_json(raw_content)
            elif language == "xml":
                media_type = "application/xml"
                schema = {"type": "string"}
            else:
                media_type = "text/plain"
                schema = {"type": "string"}

            content[media_type] = MediaType(
                media_type=media_type,
                schema=schema,
                example=raw_content,
            )

        elif mode == "urlencoded":
            urlencoded = body.get("urlencoded", [])
            properties = {}

            for item in urlencoded:
                if not isinstance(item, dict):
                    continue
                key = item.get("key", "")
                if key:
                    properties[key] = {
                        "type": "string",
                        "description": item.get("description", ""),
                    }

            content["application/x-www-form-urlencoded"] = MediaType(
                media_type="application/x-www-form-urlencoded",
                schema={"type": "object", "properties": properties},
            )

        elif mode == "formdata":
            formdata = body.get("formdata", [])
            properties = {}

            for item in formdata:
                if not isinstance(item, dict):
                    continue
                key = item.get("key", "")
                item_type = item.get("type", "text")

                if key:
                    if item_type == "file":
                        properties[key] = {
                            "type": "string",
                            "format": "binary",
                            "description": item.get("description", ""),
                        }
                    else:
                        properties[key] = {
                            "type": "string",
                            "description": item.get("description", ""),
                        }

            content["multipart/form-data"] = MediaType(
                media_type="multipart/form-data",
                schema={"type": "object", "properties": properties},
            )

        if not content:
            return None

        return RequestBody(
            description="",
            required=True,
            content=content,
        )

    def _infer_schema_from_json(self, json_str: str) -> dict[str, Any]:
        """Infers schema from JSON string."""
        import json

        try:
            data = json.loads(json_str)
            return self._infer_schema(data)
        except json.JSONDecodeError:
            return {"type": "object"}

    def _infer_schema(self, data: Any) -> dict[str, Any]:
        """Infers schema from data type."""
        if data is None:
            return {"type": "null"}
        elif isinstance(data, bool):
            return {"type": "boolean"}
        elif isinstance(data, int):
            return {"type": "integer"}
        elif isinstance(data, float):
            return {"type": "number"}
        elif isinstance(data, str):
            return {"type": "string"}
        elif isinstance(data, list):
            if data:
                return {"type": "array", "items": self._infer_schema(data[0])}
            return {"type": "array", "items": {}}
        elif isinstance(data, dict):
            properties = {k: self._infer_schema(v) for k, v in data.items()}
            return {"type": "object", "properties": properties}
        else:
            return {"type": "string"}

    def _parse_responses(self, item: dict[str, Any]) -> dict[str, Response]:
        """Parses response examples."""
        responses = {}

        for response in item.get("response", []):
            if not isinstance(response, dict):
                continue

            status_code = str(response.get("code", 200))
            name = response.get("name", "")
            body = response.get("body", "")

            # Extract schema
            schema = {}
            if body:
                schema = self._infer_schema_from_json(body)

            content = {}
            if schema:
                content["application/json"] = MediaType(
                    media_type="application/json",
                    schema=schema,
                    example=body,
                )

            responses[status_code] = Response(
                status_code=status_code,
                description=name,
                content=content,
            )

        # Default 200 response
        if not responses:
            responses["200"] = Response(
                status_code="200",
                description="Successful response",
            )

        return responses

    def _parse_request_auth(self, request: dict[str, Any]) -> list[SecurityRequirement]:
        """Parses request-level auth."""
        security = []
        auth = request.get("auth", {})

        if not auth:
            return security

        auth_type = auth.get("type", "")

        if auth_type == "apikey":
            security.append(SecurityRequirement(scheme_name="apiKey", scopes=[]))
        elif auth_type == "bearer":
            security.append(SecurityRequirement(scheme_name="bearerAuth", scopes=[]))
        elif auth_type == "basic":
            security.append(SecurityRequirement(scheme_name="basicAuth", scopes=[]))
        elif auth_type == "oauth2":
            security.append(SecurityRequirement(scheme_name="oauth2", scopes=[]))

        return security

    def _extract_tags(self, items: list[dict[str, Any]]) -> list[dict[str, str]]:
        """Extracts tags from folders."""
        tags = []

        for item in items:
            if not isinstance(item, dict):
                continue

            if "item" in item:
                name = item.get("name", "")
                description = item.get("description", "")

                if name:
                    tags.append({"name": name, "description": description})

                # Nested folders
                nested_tags = self._extract_tags(item["item"])
                tags.extend(nested_tags)

        return tags
