"""Tests for the ``x-llm-context`` path.

This is the code that makes Warp's catalog reach an MCP client, and until now
nothing exercised it — the whole reason the extension exists was untested. It
is also the context source for the benchmark's catalog arm, so a silent
regression here would show up as "the catalog does not help" rather than as a
failure.
"""

from truejoin.adapters.outbound.mcp_gateway.parsers.openapi import OpenAPIParser
from truejoin.application.mcpgen.generators.tools import ToolsGenerator
from truejoin.domain.mcp.api import HTTPMethod, ServerInfo, UnifiedAPISchema, UnifiedEndpoint

LLM_CONTEXT = {
    "table": "orders",
    "human_name": "Customer orders",
    "description": "One row per placed order.",
    "tags": ["sales", "core"],
    "row_count": 1234,
    "columns": [
        {
            "name": "amount",
            "type": "numeric",
            "description": "Order total in minor units",
            "semantic_type": "amount",
            "examples": [100, 250, 375, 400, 999, 1200],
            "tags": ["money"],
        },
        {
            "name": "customer_id",
            "type": "integer",
            "foreign_key": "customers.id",
        },
    ],
    "relationships": [
        {"from": "customer_id", "to_table": "customers", "to_column": "id", "type": "many-to-one"}
    ],
}


def endpoint(**overrides) -> UnifiedEndpoint:
    base = {
        "path": "/orders",
        "method": HTTPMethod.GET,
        "operation_id": "list_orders",
        "extensions": {"x-llm-context": LLM_CONTEXT},
    }
    return UnifiedEndpoint(**{**base, **overrides})


def schema_with(*endpoints: UnifiedEndpoint) -> UnifiedAPISchema:
    return UnifiedAPISchema(
        title="Warp Engine",
        version="1.0.0",
        servers=[ServerInfo(url="https://api.example.com")],
        endpoints=list(endpoints),
    )


class TestDescriptionEnrichment:
    def tool(self, **overrides):
        return ToolsGenerator().generate(schema_with(endpoint(**overrides)))[0]

    def test_the_catalog_reaches_the_tool_description(self):
        description = self.tool().description
        assert "Customer orders" in description
        assert "One row per placed order." in description
        assert "Tags: sales, core" in description
        assert "Columns: amount, customer_id" in description
        assert "~1234 rows" in description

    def test_relationships_are_rendered_for_the_model(self):
        # What lets a model join without guessing at the foreign key.
        assert "customer_id → customers.id (many-to-one)" in self.tool().description

    def test_a_relationship_description_wins_over_the_generated_form(self):
        context = {**LLM_CONTEXT, "relationships": [{"description": "Each order has one buyer"}]}
        tool = self.tool(extensions={"x-llm-context": context})
        assert "Each order has one buyer" in tool.description

    def test_a_plain_string_relationship_is_accepted(self):
        context = {**LLM_CONTEXT, "relationships": ["orders belong to customers"]}
        assert (
            "orders belong to customers"
            in self.tool(extensions={"x-llm-context": context}).description
        )

    def test_an_explicit_summary_wins_over_the_human_name(self):
        assert self.tool(summary="List every order").description.startswith("List every order")

    def test_without_the_extension_the_description_is_just_the_endpoint(self):
        tool = self.tool(extensions={}, summary="List orders")
        assert tool.description == "List orders | [GET /orders]"

    def test_the_raw_context_is_carried_on_the_tool(self):
        assert self.tool().x_llm_context == LLM_CONTEXT


class TestInputSchemaEnrichment:
    def properties(self, **overrides):
        from truejoin.domain.mcp.api import Parameter, ParameterLocation

        ep = endpoint(
            parameters=[
                Parameter(
                    name="amount", location=ParameterLocation.QUERY, schema={"type": "number"}
                ),
                Parameter(
                    name="customer_id", location=ParameterLocation.QUERY, schema={"type": "integer"}
                ),
            ],
            **overrides,
        )
        return ToolsGenerator().generate(schema_with(ep))[0].input_schema["properties"]

    def test_column_descriptions_land_on_the_matching_property(self):
        assert self.properties()["amount"]["description"] == "Order total in minor units"

    def test_the_semantic_type_is_published_as_a_vendor_extension(self):
        # This is what a governance layer downstream keys masking on.
        assert self.properties()["amount"]["x-semantic-type"] == "amount"

    def test_foreign_keys_are_published(self):
        assert self.properties()["customer_id"]["x-foreign-key"] == "customers.id"

    def test_tags_are_published(self):
        assert self.properties()["amount"]["x-tags"] == ["money"]

    def test_examples_are_capped(self):
        # Six were supplied; a prompt does not need all of them.
        assert len(self.properties()["amount"]["examples"]) == 5

    def test_a_property_the_catalog_does_not_mention_is_untouched(self):
        assert "x-semantic-type" not in self.properties()["customer_id"]

    def test_without_the_extension_nothing_is_added(self):
        # The property keeps its own (empty) description from the parameter;
        # what must not appear is anything the catalog would have supplied.
        plain = self.properties(extensions={})
        assert not plain["amount"].get("description")
        assert "x-semantic-type" not in plain["amount"]
        assert "examples" not in plain["amount"]


class TestFromARealWarpSpec:
    """The shape Warp actually emits, parsed end to end."""

    SPEC = {
        "openapi": "3.0.0",
        "info": {"title": "Warp Engine", "version": "1.0.0"},
        "x-llm-context": {"database": "primary_db", "database_type": "postgresql"},
        "paths": {
            "/api/v1/primary_db/orders": {
                "get": {
                    "operationId": "list_orders",
                    "x-llm-context": LLM_CONTEXT,
                    "responses": {"200": {"description": "ok"}},
                }
            }
        },
    }

    def test_the_parser_collects_both_levels(self):
        schema = OpenAPIParser().parse(self.SPEC)
        assert schema.extensions["x-llm-context"]["database"] == "primary_db"
        assert schema.endpoints[0].extensions["x-llm-context"]["table"] == "orders"

    def test_and_the_generator_uses_the_operation_level_one(self):
        schema = OpenAPIParser().parse(self.SPEC)
        tool = ToolsGenerator().generate(schema)[0]
        assert "Customer orders" in tool.description
        assert tool.x_llm_context["table"] == "orders"
