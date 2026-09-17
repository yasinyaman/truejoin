"""JSON Schema helpers."""

import copy
import logging
from typing import Any

logger = logging.getLogger(__name__)


def resolve_refs(
    schema: dict[str, Any],
    definitions: dict[str, Any],
    max_depth: int = 10,
    _current_depth: int = 0,
) -> dict[str, Any]:
    """Resolves JSON Schema $ref references.

    Args:
        schema: Schema to resolve
        definitions: Reference definitions (components/schemas)
        max_depth: Maximum recursion depth
        _current_depth: Current depth (internal use)

    Returns:
        Schema with resolved references
    """
    if _current_depth >= max_depth:
        logger.warning("Maximum ref resolution depth reached (depth=%s)", max_depth)
        return schema

    if not isinstance(schema, dict):
        return schema

    result = {}

    for key, value in schema.items():
        if key == "$ref" and isinstance(value, str):
            # #/components/schemas/Pet -> Pet
            ref_path = value.split("/")[-1]
            if ref_path in definitions:
                # Resolve reference and continue recursively
                resolved = copy.deepcopy(definitions[ref_path])
                return resolve_refs(resolved, definitions, max_depth, _current_depth + 1)
            else:
                logger.warning("Reference not found: %s", value)
                return {"type": "object"}
        elif isinstance(value, dict):
            result[key] = resolve_refs(value, definitions, max_depth, _current_depth + 1)
        elif isinstance(value, list):
            result[key] = [
                resolve_refs(item, definitions, max_depth, _current_depth + 1)
                if isinstance(item, dict)
                else item
                for item in value
            ]
        else:
            result[key] = value

    return result


def flatten_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Flattens schema (allOf, oneOf, anyOf merging)."""
    if not isinstance(schema, dict):
        return schema

    result = copy.deepcopy(schema)

    # allOf merging
    if "allOf" in result:
        merged = {}
        merged_properties = {}
        merged_required = []

        for sub_schema in result["allOf"]:
            flat_sub = flatten_schema(sub_schema)
            if "properties" in flat_sub:
                merged_properties.update(flat_sub["properties"])
            if "required" in flat_sub:
                merged_required.extend(flat_sub["required"])
            # Merge other fields too
            for k, v in flat_sub.items():
                if k not in ("properties", "required"):
                    merged[k] = v

        merged["type"] = "object"
        if merged_properties:
            merged["properties"] = merged_properties
        if merged_required:
            merged["required"] = list(set(merged_required))

        del result["allOf"]
        result.update(merged)

    # Simple approach for oneOf/anyOf: use first schema
    for key in ("oneOf", "anyOf"):
        if key in result and result[key]:
            first = flatten_schema(result[key][0])
            del result[key]
            result.update(first)

    # Flatten nested properties
    if "properties" in result:
        result["properties"] = {k: flatten_schema(v) for k, v in result["properties"].items()}

    # Flatten items (for array)
    if "items" in result:
        result["items"] = flatten_schema(result["items"])

    return result


def schema_to_mcp_input(
    parameters: list[dict[str, Any]],
    request_body_schema: dict[str, Any] | None = None,
    definitions: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Converts API parameters to MCP inputSchema.

    Args:
        parameters: API parameters
        request_body_schema: Request body schema
        definitions: Schema definitions

    Returns:
        MCP inputSchema
    """
    if definitions is None:
        definitions = {}

    properties: dict[str, Any] = {}
    required: list[str] = []

    # Add parameters
    for param in parameters:
        name = param.get("name", "")
        if not name:
            continue

        # Make parameter name suitable for MCP
        # Add prefix to distinguish path/query/header parameters
        location = param.get("in", param.get("location", "query"))
        mcp_name = name

        # Resolve schema
        param_schema = param.get("schema", {"type": "string"})
        if definitions:
            param_schema = resolve_refs(param_schema, definitions)
        param_schema = flatten_schema(param_schema)

        # Add description
        description = param.get("description", "")
        if location != "query":
            description = f"[{location}] {description}".strip()

        properties[mcp_name] = {
            **param_schema,
            "description": description,
        }

        if param.get("required", False):
            required.append(mcp_name)

    # Add request body
    if request_body_schema:
        if definitions:
            body_schema = resolve_refs(request_body_schema, definitions)
        else:
            body_schema = request_body_schema
        body_schema = flatten_schema(body_schema)

        if body_schema.get("type") == "object" and "properties" in body_schema:
            # Add body properties directly
            for prop_name, prop_schema in body_schema.get("properties", {}).items():
                if prop_name not in properties:  # If not conflicting with parameter
                    properties[prop_name] = prop_schema

            # Add required fields
            for req in body_schema.get("required", []):
                if req not in required:
                    required.append(req)
        else:
            # Add entire body as "body" parameter
            properties["body"] = {
                **body_schema,
                "description": "Request body",
            }

    result: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }

    if required:
        result["required"] = required

    return result
