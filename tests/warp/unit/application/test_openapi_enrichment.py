"""Tests for integration modules (OpenAPIEnricher, MCPEnricher)."""

import json

import pytest

from truejoin.application.catalog.openapi_enrichment import OpenAPIEnricher, withheld_from_spec
from truejoin.application.settings import SettingsConfig
from truejoin.domain.catalog.catalog import (
    ColumnCatalogEntry,
    DatabaseCatalog,
    LocalizedText,
    RelationshipInfo,
    TableCatalogEntry,
)


@pytest.fixture
def catalog():
    return DatabaseCatalog(
        database_name="testdb",
        tables={
            "users": TableCatalogEntry(
                table_name="users",
                description=LocalizedText(texts={"en": "User accounts"}),
                human_name=LocalizedText(texts={"en": "Users"}),
                columns=[
                    ColumnCatalogEntry(
                        name="id",
                        data_type="integer",
                        is_primary_key=True,
                        semantic_type="id",
                        description=LocalizedText(texts={"en": "User ID"}),
                    ),
                    ColumnCatalogEntry(
                        name="email",
                        data_type="varchar",
                        semantic_type="email",
                        description=LocalizedText(texts={"en": "User email"}),
                    ),
                ],
                primary_key="id",
                relationships=[
                    RelationshipInfo(
                        source_column="id",
                        target_table="orders",
                        target_column="user_id",
                        relationship_type="one-to-many",
                        description=LocalizedText(texts={"en": "User has orders"}),
                    )
                ],
            ),
        },
    )


class TestOpenAPIEnricher:
    def test_enrich_basic(self, catalog):
        spec = {
            "paths": {
                "/api/v1/users": {
                    "get": {
                        "summary": "List users",
                        "parameters": [],
                    },
                },
                "/api/v1/users/{id}": {
                    "get": {
                        "summary": "Get user",
                        "parameters": [
                            {"name": "id", "in": "path"},
                        ],
                    },
                },
            },
            "components": {"schemas": {}},
        }

        enricher = OpenAPIEnricher(catalog, lang="en")
        result = enricher.enrich(spec)

        # Should enrich summary
        get_op = result["paths"]["/api/v1/users"]["get"]
        assert "Users" in get_op["summary"]
        # Description is now a rich markdown block led by the table summary.
        assert get_op["description"].startswith("**Users**: User accounts")

    def test_enrich_request_body(self, catalog):
        spec = {
            "paths": {
                "/api/v1/users": {
                    "post": {
                        "summary": "Create user",
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "properties": {
                                            "email": {"type": "string"},
                                        }
                                    }
                                }
                            }
                        },
                    },
                },
            },
        }

        enricher = OpenAPIEnricher(catalog, lang="en")
        result = enricher.enrich(spec)

        body_schema = result["paths"]["/api/v1/users"]["post"]["requestBody"]["content"][
            "application/json"
        ]["schema"]
        # Column descriptions now include semantic-type metadata.
        email_desc = body_schema["properties"]["email"]["description"]
        assert email_desc.startswith("User email")
        assert "Semantic type: email" in email_desc

    def test_extract_table_from_path(self, catalog):
        enricher = OpenAPIEnricher(catalog)
        assert enricher._extract_table_from_path("/api/v1/users") == "users"
        assert enricher._extract_table_from_path("/api/v1/users/{id}") == "users"
        # Only /api/v1-prefixed paths are matched; unprefixed paths are ignored.
        assert enricher._extract_table_from_path("/users") is None

    def test_extract_column_from_param(self):
        assert OpenAPIEnricher._extract_column_from_param("filter[email][eq]") == "email"
        assert OpenAPIEnricher._extract_column_from_param("sort") is None
        assert OpenAPIEnricher._extract_column_from_param("email") == "email"
        assert OpenAPIEnricher._extract_column_from_param("limit") is None

    def test_enrich_file(self, catalog, tmp_path):
        spec = {
            "openapi": "3.0.0",
            "paths": {
                "/api/v1/users": {
                    "get": {"summary": "List", "parameters": []},
                },
            },
        }
        input_file = tmp_path / "spec.json"
        with open(input_file, "w") as f:
            json.dump(spec, f)

        enricher = OpenAPIEnricher(catalog, lang="en")
        output_file = enricher.enrich_file(input_file)

        assert output_file.exists()
        with open(output_file) as f:
            result = json.load(f)
        assert "Users" in result["paths"]["/api/v1/users"]["get"]["summary"]


class TestOpenAPIExamplesPolicy:
    """Sample values are only written into the spec when explicitly enabled."""

    def _spec(self):
        return {
            "paths": {
                "/api/v1/users": {
                    "get": {
                        "summary": "List users",
                        "parameters": [{"name": "filter[email]", "in": "query"}],
                    },
                    "post": {
                        "summary": "Create user",
                        "requestBody": {
                            "content": {
                                "application/json": {
                                    "schema": {"properties": {"email": {"type": "string"}}}
                                }
                            }
                        },
                    },
                }
            },
            "components": {
                "schemas": {
                    "warp__schema__analyzer__UsersResponse": {
                        "properties": {"email": {"type": "string"}}
                    }
                }
            },
        }

    @staticmethod
    def _find_keys(obj, keys):
        found = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k in keys:
                    found.append((k, v))
                found.extend(TestOpenAPIExamplesPolicy._find_keys(v, keys))
        elif isinstance(obj, list):
            for v in obj:
                found.extend(TestOpenAPIExamplesPolicy._find_keys(v, keys))
        return found

    @staticmethod
    def _with_samples(catalog):
        col = catalog.tables["users"].get_column("email")
        assert col is not None
        col.sample_values = ["a@x.com", "b@x.com"]
        return catalog

    def test_no_examples_by_default(self, catalog):
        result = OpenAPIEnricher(self._with_samples(catalog), lang="en").enrich(self._spec())
        assert self._find_keys(result, {"example", "examples"}) == []
        assert "Examples:" not in json.dumps(result)
        # Descriptions are still injected.
        assert "x-llm-context" in result

    def _examples(self, catalog, **kwargs):
        id_col = catalog.tables["users"].get_column("id")
        assert id_col is not None
        id_col.sample_values = [4101, 4102]
        result = OpenAPIEnricher(
            self._with_samples(catalog), lang="en", include_examples=True, **kwargs
        ).enrich(self._spec())
        return json.dumps(self._find_keys(result, {"example", "examples"}))

    def test_examples_when_enabled(self, catalog):
        assert "4101" in self._examples(catalog)

    def test_personal_columns_never_get_examples(self, catalog):
        """The spec is one document for every caller, often without a key."""
        found = self._examples(catalog)
        assert "a@x.com" not in found
        assert "4101" in found
        # Only the spec is stripped; the catalog keeps its values.
        assert catalog.tables["users"].get_column("email").sample_values == ["a@x.com", "b@x.com"]

    def test_a_column_named_like_a_national_id_gets_none(self, catalog):
        catalog.tables["users"].columns.append(
            ColumnCatalogEntry(name="tc_kimlik", data_type="varchar", sample_values=["10000000146"])
        )
        assert "10000000146" not in self._examples(catalog)

    def test_masked_types_and_row_ruled_tables_get_none(self, catalog):
        assert "4101" not in self._examples(catalog, withheld_types={"id"})
        assert "4101" not in self._examples(catalog, withheld_tables={"USERS"})

    def test_what_the_settings_withhold(self):
        settings = SettingsConfig.model_validate(
            {
                "auth": {
                    "enabled": True,
                    "api_keys": [
                        {
                            "key": "k" * 32,
                            "name": "t",
                            "row_filters": {"orders": [{"column": "tenant", "value": "a"}]},
                        },
                        {"key": "j" * 32, "name": "u", "row_filters": {"empty": []}},
                    ],
                },
                "masking": {
                    "enabled": True,
                    "rules": {"email": "redact"},
                    "by_role": {"auditor": {"iban": "last4"}},
                },
            }
        )
        withheld = withheld_from_spec(settings)
        assert withheld["withheld_types"] == {"email", "iban"}
        assert withheld["withheld_tables"] == {"orders"}
        settings.masking.enabled = False
        assert withheld_from_spec(settings)["withheld_types"] == set()


class TestMultiDatabaseContext:
    """Several catalogs enrich one spec into a single, merged x-llm-context."""

    @staticmethod
    def _catalog(name: str) -> DatabaseCatalog:
        return DatabaseCatalog(
            database_name=name,
            tables={"users": TableCatalogEntry(table_name="users")},
        )

    def test_single_catalog_keeps_flat_shape(self):
        spec = OpenAPIEnricher(self._catalog("a")).enrich({"paths": {}})
        assert spec["x-llm-context"]["database"] == "a"
        assert "databases" not in spec["x-llm-context"]

    def test_an_approved_catalogs_notes_and_glossary_are_published(self):
        from truejoin.domain.catalog.catalog import CatalogStatus

        guided = self._catalog("a")
        guided.glossary, guided.notes = {"SP": "sipariş"}, ["Amounts are in TRY."]
        draft = OpenAPIEnricher(guided).enrich({"paths": {}})["x-llm-context"]
        assert "notes" not in draft and "glossary" not in draft  # nobody approved them
        guided.status = CatalogStatus.approved
        context = OpenAPIEnricher(guided).enrich({"paths": {}})["x-llm-context"]
        assert context["notes"] == ["Amounts are in TRY."]
        assert context["glossary"] == {"SP": "sipariş"}

    def test_two_catalogs_are_merged_not_overwritten(self):
        spec = {"paths": {}}
        OpenAPIEnricher(self._catalog("a")).enrich(spec)
        OpenAPIEnricher(self._catalog("b")).enrich(spec)
        ctx = spec["x-llm-context"]
        assert [d["database"] for d in ctx["databases"]] == ["a", "b"]
        assert "database" not in ctx

    def test_re_enriching_same_database_replaces_its_entry(self):
        spec = {"paths": {}}
        OpenAPIEnricher(self._catalog("a")).enrich(spec)
        OpenAPIEnricher(self._catalog("b")).enrich(spec)
        OpenAPIEnricher(self._catalog("a")).enrich(spec)
        assert [d["database"] for d in spec["x-llm-context"]["databases"]] == ["b", "a"]
        spec2 = {"paths": {}}
        OpenAPIEnricher(self._catalog("a")).enrich(spec2)
        OpenAPIEnricher(self._catalog("a")).enrich(spec2)
        assert spec2["x-llm-context"]["database"] == "a"  # still flat, no duplicate


class TestStructureWithoutACatalog:
    """Types, keys and relationships are the engine's own words, not a model's.

    They used to travel inside the catalog, so `x-llm-context` carried nothing
    at all until an LLM pass had run over every table and a reviewer had
    approved it. On a schema with a couple of thousand tables that is weeks
    before a consumer can see that one column points at another table.
    """

    @staticmethod
    def _schema():
        from truejoin.domain.gateway.schema import (
            ColumnSchema,
            DatabaseSchema,
            ForeignKeySchema,
            TableSchema,
        )

        return DatabaseSchema(
            database_name="erp",
            tables={
                "siparis_satirlari": TableSchema(
                    table_name="siparis_satirlari",
                    columns=[
                        ColumnSchema(name="siparis_id", type="integer", nullable=False),
                        ColumnSchema(name="satir_no", type="integer", nullable=False),
                        ColumnSchema(name="urun_id", type="integer", nullable=True),
                    ],
                    primary_key=["siparis_id", "satir_no"],
                    foreign_keys=[
                        ForeignKeySchema(
                            column="urun_id",
                            references_table="urunler",
                            references_column="id",
                        )
                    ],
                )
            },
        )

    def test_it_carries_the_whole_composite_key(self):
        from truejoin.domain.catalog.catalog import structural_catalog

        table = structural_catalog("erp", self._schema()).tables["siparis_satirlari"]
        keys = {c.name for c in table.columns if c.is_primary_key}

        assert keys == {"siparis_id", "satir_no"}
        assert table.primary_key == ["siparis_id", "satir_no"]

    def test_it_carries_relationships(self):
        from truejoin.domain.catalog.catalog import structural_catalog

        table = structural_catalog("erp", self._schema()).tables["siparis_satirlari"]
        urun = next(c for c in table.columns if c.name == "urun_id")

        assert urun.is_foreign_key is True
        assert urun.references == "urunler.id"
        assert urun.nullable is True

    def test_it_claims_no_meaning(self):
        """Descriptions and semantic types are the half that needs a model."""
        from truejoin.domain.catalog.catalog import structural_catalog

        table = structural_catalog("erp", self._schema()).tables["siparis_satirlari"]

        assert all(column.semantic_type is None for column in table.columns)
        assert all(not column.description.texts for column in table.columns)

    def test_it_is_not_approved(self):
        """So nothing that gates on review can be satisfied by it.

        Masking keys on semantic types and only reads an approved catalog. A
        structural stand-in has neither, and must not look like it does.
        """
        from truejoin.domain.catalog.catalog import CatalogStatus, structural_catalog
        from truejoin.domain.governance.masking import semantic_types_of

        catalog = structural_catalog("erp", self._schema())

        assert catalog.status != CatalogStatus.approved
        assert semantic_types_of(catalog) == {}


class TestCertifiedTerms:
    """The published context carries the words of a term only once it is certified."""

    def _context(self, catalog, certified: bool, approved: bool = True) -> dict:
        from truejoin.domain.catalog.catalog import CatalogStatus
        from truejoin.domain.catalog.terms import ColumnAttribute, Term

        catalog.terms = {
            "User": Term(
                name="User",
                synonyms=["member"],
                tables=["users"],
                column_attributes=[
                    ColumnAttribute(table="users", column="email", synonyms=["mail"]),
                    ColumnAttribute(table="users", column="id"),
                ],
                certified=certified,
            )
        }
        catalog.status = CatalogStatus.approved if approved else CatalogStatus.draft
        spec = OpenAPIEnricher(catalog).enrich({"paths": {}, "components": {"schemas": {}}})
        return spec["x-llm-context"]["tables"][0]

    def test_a_certified_term_is_published_with_its_words(self, catalog):
        assert self._context(catalog, certified=True)["term"] == {
            "name": "User",
            "synonyms": ["member"],
            "columns": {"email": ["mail"]},
        }

    def test_an_uncertified_one_or_a_draft_catalog_publishes_none(self, catalog):
        assert "term" not in self._context(catalog, certified=False)
        assert "term" not in self._context(catalog, certified=True, approved=False)
