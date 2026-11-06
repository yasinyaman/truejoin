"""End-to-end tests for the catalog REST API router.

Drives create_catalog_router with a real CatalogFileStore (tmp_path) and a
seeded DatabaseCatalog via fastapi.testclient.TestClient. The /analyze endpoint
(which needs an LLM) is exercised only for its early error paths.
"""

import dataclasses
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from trio.adapters.inbound.http.auth import AuthManager
from trio.adapters.inbound.http.routes.catalog import (
    _refresh_openapi_enrichment,
    create_catalog_router,
)
from trio.adapters.outbound.catalog_store.file_store import CatalogFileStore
from trio.application.catalog.catalog_review import CatalogReviewService
from trio.application.settings import ApiKeyConfig, AuthConfig, DatabaseConfig, Settings
from trio.bootstrap import build_container
from trio.domain.catalog.catalog import (
    ColumnCatalogEntry,
    DatabaseCatalog,
    LocalizedText,
    RelationshipInfo,
    TableCatalogEntry,
)


def _make_catalog() -> DatabaseCatalog:
    return DatabaseCatalog(
        database_name="testdb",
        database_type="postgresql",
        description=LocalizedText(texts={"en": "Test database", "tr": "Test veritabani"}),
        tables={
            "users": TableCatalogEntry(
                table_name="users",
                description=LocalizedText(texts={"en": "User accounts"}),
                human_name=LocalizedText(texts={"en": "Users", "tr": "Kullanicilar"}),
                columns=[
                    ColumnCatalogEntry(
                        name="id",
                        data_type="integer",
                        is_primary_key=True,
                        description=LocalizedText(texts={"en": "Primary key"}),
                    ),
                    ColumnCatalogEntry(
                        name="email",
                        data_type="varchar",
                        semantic_type="email",
                        sample_values=["a@x.com"],
                        description=LocalizedText(texts={"en": "Email"}),
                    ),
                ],
                primary_key="id",
                row_count=1000,
                tags=["core"],
                relationships=[
                    RelationshipInfo(
                        source_column="id",
                        target_table="orders",
                        target_column="user_id",
                        relationship_type="one-to-many",
                        description=LocalizedText(texts={"en": "User orders"}),
                    )
                ],
            ),
            "orders": TableCatalogEntry(
                table_name="orders",
                description=LocalizedText(texts={"en": "Customer orders"}),
                columns=[
                    ColumnCatalogEntry(name="id", data_type="integer", is_primary_key=True),
                    ColumnCatalogEntry(
                        name="user_id",
                        data_type="integer",
                        is_foreign_key=True,
                        references="users.id",
                    ),
                ],
                primary_key="id",
            ),
        },
        languages=["en", "tr"],
        llm_provider="openai",
        llm_model="gpt-4o-mini",
    )


@pytest.fixture
def store(tmp_path: Path) -> CatalogFileStore:
    s = CatalogFileStore(tmp_path)
    CatalogReviewService(s).save_as_draft(_make_catalog())
    return s


def _container(store: CatalogFileStore, settings: Settings | None = None):
    """Container over the test store; other adapters keep their defaults (unused here)."""
    return build_container(settings or Settings(), repository=store)


@pytest.fixture
def client(store: CatalogFileStore) -> TestClient:
    app = FastAPI()
    router = create_catalog_router(container=_container(store), gateways={}, app=app)
    app.include_router(router)
    return TestClient(app)


class TestListAndInfo:
    def test_list_catalogs(self, client: TestClient) -> None:
        r = client.get("/catalog")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 1
        names = [c["database_name"] for c in body["catalogs"]]
        assert "testdb" in names

    def test_list_catalogs_empty(self, tmp_path: Path) -> None:
        app = FastAPI()
        empty_store = CatalogFileStore(tmp_path / "empty")
        app.include_router(create_catalog_router(container=_container(empty_store)))
        c = TestClient(app)
        r = c.get("/catalog")
        assert r.status_code == 200
        assert r.json() == {"catalogs": [], "total": 0, "connected": []}

    def test_list_names_the_databases_that_can_be_analyzed(self, store) -> None:
        app = FastAPI()
        gateways = {"zdb": object(), "testdb": object()}
        app.include_router(create_catalog_router(container=_container(store), gateways=gateways))
        assert TestClient(app).get("/catalog").json()["connected"] == ["testdb", "zdb"]

    def test_get_catalog_info(self, client: TestClient) -> None:
        r = client.get("/catalog/testdb")
        assert r.status_code == 200
        body = r.json()
        assert body["database_name"] == "testdb"
        assert body["table_count"] == 2
        assert body["llm_provider"] == "openai"
        tables = {t["table_name"]: t for t in body["tables"]}
        assert tables["users"]["human_name"] == "Users"
        assert tables["users"]["column_count"] == 2

    def test_get_catalog_info_lang(self, client: TestClient) -> None:
        r = client.get("/catalog/testdb", params={"lang": "tr"})
        assert r.status_code == 200
        tables = {t["table_name"]: t for t in r.json()["tables"]}
        assert tables["users"]["human_name"] == "Kullanicilar"

    def test_get_catalog_info_404(self, client: TestClient) -> None:
        r = client.get("/catalog/nope")
        assert r.status_code == 404
        assert "nope" in r.json()["detail"]


class TestExport:
    def test_export_json(self, client: TestClient) -> None:
        r = client.get("/catalog/testdb/export", params={"format": "json"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("application/json")
        assert "attachment" in r.headers["content-disposition"]
        assert r.json()["database_name"] == "testdb"

    def test_export_yaml(self, client: TestClient) -> None:
        r = client.get("/catalog/testdb/export", params={"format": "yaml"})
        assert r.status_code == 200
        assert "yaml" in r.headers["content-type"]
        assert "database_name: testdb" in r.text

    def test_export_markdown(self, client: TestClient) -> None:
        r = client.get("/catalog/testdb/export", params={"format": "markdown"})
        assert r.status_code == 200
        assert "markdown" in r.headers["content-type"]
        assert "# testdb" in r.text

    def test_export_bad_format(self, client: TestClient) -> None:
        r = client.get("/catalog/testdb/export", params={"format": "xml"})
        assert r.status_code == 400

    def test_export_404(self, client: TestClient) -> None:
        r = client.get("/catalog/nope/export")
        assert r.status_code == 404


class TestDraftReview:
    def test_get_draft_info(self, client: TestClient) -> None:
        r = client.get("/catalog/testdb/draft")
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "draft"
        assert body["review_summary"].get("pending") == 2
        assert len(body["tables"]) == 2

    def test_get_draft_info_404(self, client: TestClient) -> None:
        assert client.get("/catalog/nope/draft").status_code == 404

    def test_get_draft_table(self, client: TestClient) -> None:
        r = client.get("/catalog/testdb/draft/tables/users")
        assert r.status_code == 200
        body = r.json()
        assert body["table_name"] == "users"
        assert body["column_count"] == 2
        assert len(body["relationships"]) == 1
        cols = {c["name"]: c for c in body["columns"]}
        assert cols["email"]["semantic_type"] == "email"

    def test_get_draft_table_catalog_404(self, client: TestClient) -> None:
        assert client.get("/catalog/nope/draft/tables/users").status_code == 404

    def test_get_draft_table_table_404(self, client: TestClient) -> None:
        r = client.get("/catalog/testdb/draft/tables/ghost")
        assert r.status_code == 404


class TestEdit:
    def test_edit_table(self, client: TestClient) -> None:
        r = client.patch(
            "/catalog/testdb/draft/tables/users",
            json={"description": {"en": "Edited desc"}, "tags": ["a", "b"]},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["review_status"] == "modified"
        assert set(body["updated_fields"]) == {"description", "tags"}
        # Verify persisted
        detail = client.get("/catalog/testdb/draft/tables/users").json()
        assert detail["description"]["en"] == "Edited desc"
        assert detail["tags"] == ["a", "b"]

    def test_edit_table_no_fields(self, client: TestClient) -> None:
        r = client.patch("/catalog/testdb/draft/tables/users", json={})
        assert r.status_code == 400

    def test_edit_table_catalog_404(self, client: TestClient) -> None:
        r = client.patch("/catalog/nope/draft/tables/users", json={"tags": ["x"]})
        assert r.status_code == 404

    def test_edit_table_not_draft(self, client: TestClient, store: CatalogFileStore) -> None:
        CatalogReviewService(store).approve_catalog("testdb")
        r = client.patch("/catalog/testdb/draft/tables/users", json={"tags": ["x"]})
        assert r.status_code == 409

    def test_edit_table_unknown_table(self, client: TestClient) -> None:
        r = client.patch("/catalog/testdb/draft/tables/ghost", json={"tags": ["x"]})
        assert r.status_code == 400

    def test_edit_column(self, client: TestClient) -> None:
        r = client.patch(
            "/catalog/testdb/draft/tables/users/columns/email",
            json={"semantic_type": "contact_email", "description": {"en": "the email"}},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["column_name"] == "email"
        assert body["semantic_type"] == "contact_email"

    def test_edit_column_no_fields(self, client: TestClient) -> None:
        r = client.patch("/catalog/testdb/draft/tables/users/columns/email", json={})
        assert r.status_code == 400

    def test_edit_column_catalog_404(self, client: TestClient) -> None:
        r = client.patch(
            "/catalog/nope/draft/tables/users/columns/email",
            json={"semantic_type": "x"},
        )
        assert r.status_code == 404

    def test_edit_column_not_draft(self, client: TestClient, store: CatalogFileStore) -> None:
        CatalogReviewService(store).approve_catalog("testdb")
        r = client.patch(
            "/catalog/testdb/draft/tables/users/columns/email",
            json={"semantic_type": "x"},
        )
        assert r.status_code == 409

    def test_edit_column_unknown(self, client: TestClient) -> None:
        r = client.patch(
            "/catalog/testdb/draft/tables/users/columns/ghost",
            json={"semantic_type": "x"},
        )
        assert r.status_code == 400


class TestApprove:
    def test_approve_specific_tables(self, client: TestClient) -> None:
        r = client.post("/catalog/testdb/approve", json={"tables": ["users"]})
        assert r.status_code == 200
        body = r.json()
        # only one of two approved -> catalog stays draft
        assert body["status"] == "draft"
        assert body["review_summary"].get("approved") == 1

    def test_approve_all(self, client: TestClient) -> None:
        r = client.post("/catalog/testdb/approve", json={})
        assert r.status_code == 200
        assert r.json()["status"] == "approved"

    def test_approve_specific_then_all(self, client: TestClient) -> None:
        client.post("/catalog/testdb/approve", json={"tables": ["users"]})
        r = client.post("/catalog/testdb/approve", json={"tables": ["orders"]})
        assert r.status_code == 200
        assert r.json()["status"] == "approved"

    def test_approve_404(self, client: TestClient) -> None:
        r = client.post("/catalog/nope/approve", json={})
        assert r.status_code == 404

    def test_approve_unknown_table_400(self, client: TestClient) -> None:
        r = client.post("/catalog/testdb/approve", json={"tables": ["ghost"]})
        assert r.status_code == 400

    def test_approve_single_table(self, client: TestClient) -> None:
        r = client.post("/catalog/testdb/approve/users")
        assert r.status_code == 200
        body = r.json()
        assert body["table"] == "users"
        assert body["catalog_status"] == "draft"

    def test_approve_single_table_completes_catalog(self, client: TestClient) -> None:
        client.post("/catalog/testdb/approve/users")
        r = client.post("/catalog/testdb/approve/orders")
        assert r.status_code == 200
        assert r.json()["catalog_status"] == "approved"

    def test_approve_single_table_404(self, client: TestClient) -> None:
        r = client.post("/catalog/testdb/approve/ghost")
        assert r.status_code == 400


class TestSuggest:
    """`POST /catalog/{db}/suggest` is `trio catalog suggest` over HTTP."""

    def _client(self, store, report=None, error=None, audit=None):
        from tests.warp.conftest import MockDatabaseAdapter
        from trio.application.catalog.history_mining import MiningReport

        class Miner:
            seen: dict = {}

            async def suggest(self, **kwargs):
                Miner.seen = kwargs
                if error:
                    raise error
                return report or MiningReport()

        class Gateways:
            def create(self, config):
                return MockDatabaseAdapter()

        class Model:
            async def close(self):
                pass

        settings = Settings(
            databases=[DatabaseConfig(name="testdb", type="postgresql", database="d", username="u")]
        )
        container = build_container(
            settings,
            repository=store,
            gateway_factory=Gateways(),
            text_generator_factory=Model,
            mining_factory=lambda gateway, db, model: Miner(),
        )
        if audit is not None:
            container = dataclasses.replace(container, audit=audit)
        app = FastAPI()
        app.include_router(create_catalog_router(container=container, gateways={}))
        return TestClient(app), Miner

    def test_it_stores_drafts_and_says_what_it_read(self, store) -> None:
        from trio.application.catalog.history_mining import MiningReport
        from trio.domain.catalog.terms import SqlAttribute

        draft = SqlAttribute(name="work_mail", table="users", expression="1")
        events: list = []

        class Audit:
            def record(self, event):
                events.append(event)

        report = MiningReport(statements=4, views=1, unavailable=["no grant"], added=[draft])
        client, miner = self._client(store, report, audit=Audit())
        r = client.post("/catalog/testdb/suggest", json={"limit": 5, "use_views": False})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["statements"] == 4 and body["unavailable"] == ["no grant"]
        assert body["added"][0]["name"] == "work_mail" and body["added"][0]["expression"] == "1"
        assert miner.seen == {
            "limit": 5,
            "statements": 500,
            "use_history": True,
            "use_views": False,
        }
        assert [(e.table, e.change) for e in events] == [("users", "suggest_sql work_mail")]

    def test_nothing_found_is_not_an_error(self, store) -> None:
        client, _ = self._client(store)
        assert client.post("/catalog/testdb/suggest", json={}).json()["added"] == []

    def test_without_a_catalog_it_is_404(self, store) -> None:
        client, _ = self._client(store)
        assert client.post("/catalog/ghost/suggest", json={}).status_code == 404

    def test_limits_are_checked(self, store) -> None:
        client, _ = self._client(store)
        assert client.post("/catalog/testdb/suggest", json={"limit": 0}).status_code == 422

    def test_an_unexpected_failure_says_nothing_of_its_insides(self, store) -> None:
        client, _ = self._client(store, error=RuntimeError("password=hunter2"))
        r = client.post("/catalog/testdb/suggest", json={})
        assert r.status_code == 500 and "hunter2" not in r.text


class TestDelete:
    def test_delete(self, client: TestClient) -> None:
        r = client.delete("/catalog/testdb")
        assert r.status_code == 200
        assert r.json() == {"deleted": True, "database": "testdb"}
        assert client.get("/catalog/testdb").status_code == 404

    def test_delete_404(self, client: TestClient) -> None:
        assert client.delete("/catalog/nope").status_code == 404


class TestOpenAPIEnrichmentRefresh:
    """Cover _refresh_openapi_enrichment via the approve endpoints."""

    def _client_with_enrichment(self, store: CatalogFileStore) -> TestClient:
        app = FastAPI(title="t", version="1.0.0")
        config = Settings()
        config.settings.catalog.auto_enrich_openapi = True
        router = create_catalog_router(
            container=_container(store, config), gateways={"testdb": object()}, app=app
        )
        app.include_router(router)
        return TestClient(app)

    def test_approve_all_triggers_refresh(self, store: CatalogFileStore) -> None:
        client = self._client_with_enrichment(store)
        r = client.post("/catalog/testdb/approve", json={})
        assert r.status_code == 200
        assert r.json()["status"] == "approved"
        # OpenAPI schema is now enriched/cached
        assert client.get("/openapi.json").status_code == 200

    def test_approve_single_completes_and_refreshes(self, store: CatalogFileStore) -> None:
        client = self._client_with_enrichment(store)
        client.post("/catalog/testdb/approve/users")
        r = client.post("/catalog/testdb/approve/orders")
        assert r.status_code == 200
        assert r.json()["catalog_status"] == "approved"

    def test_delete_clears_enrichment(self, store: CatalogFileStore) -> None:
        client = self._client_with_enrichment(store)
        assert client.post("/catalog/testdb/approve", json={}).status_code == 200
        assert "x-llm-context" in client.get("/openapi.json").json()

        assert client.delete("/catalog/testdb").status_code == 200
        spec = client.get("/openapi.json").json()
        assert "x-llm-context" not in spec
        assert "Users" not in str(spec["paths"].get("/api/v1/users", ""))

    def test_draft_catalog_is_not_published(self, store: CatalogFileStore) -> None:
        app = FastAPI(title="t", version="1.0.0")
        config = Settings()
        config.settings.catalog.auto_enrich_openapi = True
        container = _container(store, config)
        app.include_router(
            create_catalog_router(container=container, gateways={"testdb": object()}, app=app)
        )
        client = TestClient(app)
        assert client.post("/catalog/testdb/approve", json={}).status_code == 200
        assert "x-llm-context" in client.get("/openapi.json").json()

        # Re-opened as a draft (as a re-analysis does) -> its context is withdrawn.
        catalog = store.load_or_raise("testdb")
        CatalogReviewService(store).save_as_draft(catalog)
        _refresh_openapi_enrichment(app, container, {"testdb": object()})
        assert "x-llm-context" not in client.get("/openapi.json").json()

    def test_refresh_disabled_by_config(self, store: CatalogFileStore) -> None:
        app = FastAPI()
        config = Settings()
        config.settings.catalog.auto_enrich_openapi = False
        router = create_catalog_router(
            container=_container(store, config), gateways={"testdb": object()}, app=app
        )
        app.include_router(router)
        client = TestClient(app)
        r = client.post("/catalog/testdb/approve", json={})
        assert r.status_code == 200


class TestAnalyzeEarlyPaths:
    """Cover /analyze guards without invoking the LLM."""

    def test_analyze_no_adapters(self, store: CatalogFileStore) -> None:
        app = FastAPI()
        # config present but adapters empty -> 503
        app.include_router(create_catalog_router(container=_container(store), gateways=None))
        c = TestClient(app)
        r = c.post("/catalog/analyze", json={"database": "testdb"})
        assert r.status_code == 503

    def test_analyze_db_not_connected(self, store: CatalogFileStore) -> None:
        app = FastAPI()
        app.include_router(
            create_catalog_router(container=_container(store), gateways={"other": object()})
        )
        c = TestClient(app)
        r = c.post("/catalog/analyze", json={"database": "testdb"})
        assert r.status_code == 404

    def test_analyze_db_config_missing(self, store: CatalogFileStore) -> None:
        # adapter named "testdb" exists, but no matching db in config.databases
        app = FastAPI()
        app.include_router(
            create_catalog_router(container=_container(store), gateways={"testdb": object()})
        )
        c = TestClient(app)
        r = c.post("/catalog/analyze", json={"database": "testdb"})
        assert r.status_code == 404
        assert "config not found" in r.json()["detail"]


class TestCatalogNamePathSafety:
    """`{db_name}` path params are validated before they can reach the filesystem."""

    def test_delete_traversal_rejected(self, client: TestClient, store: CatalogFileStore) -> None:
        sibling = store.base_path.parent / "sibling"
        sibling.mkdir()
        (sibling / "keep.txt").write_text("x")

        r = client.delete("/catalog/%2e%2e")  # decodes to ".."
        assert r.status_code == 422

        assert store.base_path.exists()
        assert (sibling / "keep.txt").exists()
        assert client.get("/catalog/testdb").status_code == 200

    @pytest.mark.parametrize("name", ["%2e%2e", "_index", "a.b", "bad%20name"])
    def test_read_endpoints_reject_unsafe_names(self, client: TestClient, name: str) -> None:
        assert client.get(f"/catalog/{name}").status_code == 422
        assert client.get(f"/catalog/{name}/draft").status_code == 422
        assert client.post(f"/catalog/{name}/approve").status_code == 422


def _auth_manager() -> AuthManager:
    return AuthManager(
        AuthConfig(
            enabled=True,
            api_keys=[
                ApiKeyConfig(key="reader-key", name="reader", permissions=["read"]),
                ApiKeyConfig(key="writer-key", name="writer", permissions=["create", "update"]),
                ApiKeyConfig(key="admin-key", name="admin", permissions=["all"]),
            ],
            public_paths=["/health"],
        )
    )


@pytest.fixture
def auth_client(store: CatalogFileStore) -> TestClient:
    app = FastAPI()
    app.include_router(
        create_catalog_router(
            container=_container(store), gateways={}, app=app, auth_manager=_auth_manager()
        )
    )
    return TestClient(app)


def _h(key: str) -> dict[str, str]:
    return {"X-API-Key": key}


class TestAuth:
    """Every catalog endpoint is behind the auth manager when auth is enabled."""

    def test_no_key_is_401(self, auth_client: TestClient) -> None:
        assert auth_client.get("/catalog").status_code == 401
        assert auth_client.get("/catalog/testdb").status_code == 401
        assert auth_client.delete("/catalog/testdb").status_code == 401
        assert auth_client.post("/catalog/analyze", json={"database": "testdb"}).status_code == 401

    def test_invalid_key_is_401(self, auth_client: TestClient) -> None:
        assert auth_client.get("/catalog", headers=_h("nope")).status_code == 401

    def test_reader_can_only_read(self, auth_client: TestClient) -> None:
        h = _h("reader-key")
        assert auth_client.get("/catalog", headers=h).status_code == 200
        assert auth_client.get("/catalog/testdb", headers=h).status_code == 200
        assert auth_client.get("/catalog/testdb/draft", headers=h).status_code == 200
        assert auth_client.get("/catalog/testdb/draft/tables/users", headers=h).status_code == 200
        assert auth_client.get("/catalog/testdb/export?format=json", headers=h).status_code == 200

        assert (
            auth_client.patch(
                "/catalog/testdb/draft/tables/users", json={"tags": ["core"]}, headers=h
            ).status_code
            == 403
        )
        assert auth_client.post("/catalog/testdb/approve", headers=h).status_code == 403
        assert auth_client.post("/catalog/testdb/approve/users", headers=h).status_code == 403
        assert auth_client.delete("/catalog/testdb", headers=h).status_code == 403
        assert (
            auth_client.post("/catalog/analyze", json={"database": "testdb"}, headers=h).status_code
            == 403
        )

    def test_writer_can_edit_and_analyze_but_not_delete(self, auth_client: TestClient) -> None:
        h = _h("writer-key")
        r = auth_client.patch(
            "/catalog/testdb/draft/tables/users", json={"tags": ["core"]}, headers=h
        )
        assert r.status_code == 200
        assert auth_client.post("/catalog/testdb/approve/users", headers=h).status_code == 200
        # Auth passes for CREATE; the handler then reports that no adapters are wired (503).
        assert (
            auth_client.post("/catalog/analyze", json={"database": "testdb"}, headers=h).status_code
            == 503
        )
        assert auth_client.delete("/catalog/testdb", headers=h).status_code == 403

    def test_admin_can_delete(self, auth_client: TestClient) -> None:
        assert auth_client.delete("/catalog/testdb", headers=_h("admin-key")).status_code == 200
        assert auth_client.get("/catalog/testdb", headers=_h("admin-key")).status_code == 404

    def test_auth_disabled_keeps_endpoints_open(self, client: TestClient) -> None:
        assert client.get("/catalog").status_code == 200


class TestAnalyzeErrorBodies:
    def test_unexpected_error_is_generic(self, store: CatalogFileStore) -> None:
        from unittest.mock import patch

        settings = Settings(
            databases=[DatabaseConfig(name="testdb", type="postgresql", database="x", username="u")]
        )
        app = FastAPI()
        app.include_router(
            create_catalog_router(
                container=_container(store, settings), gateways={"testdb": object()}
            )
        )
        c = TestClient(app)
        with patch(
            "trio.adapters.outbound.llm.providers.LLMClient.from_config",
            side_effect=RuntimeError("secret internal detail: /etc/passwd"),
        ):
            r = c.post("/catalog/analyze", json={"database": "testdb"})
        assert r.status_code == 500
        assert r.json() == {"detail": "Analysis failed"}
        assert "secret" not in r.text


class TestAnalyzeReport:
    def test_response_lists_failed_and_fallback_tables(self, store: CatalogFileStore) -> None:
        from unittest.mock import AsyncMock, MagicMock

        from trio.application.catalog.catalog_analysis import AnalysisReport

        settings = Settings(
            databases=[DatabaseConfig(name="testdb", type="postgresql", database="x", username="u")]
        )
        catalog = _make_catalog()

        fake = MagicMock()
        fake.analyze = AsyncMock(return_value=catalog)
        fake.report = AnalysisReport(
            catalog=catalog, failed_tables=["broken"], fallback_tables=["orders"], llm_successes=1
        )
        container = build_container(
            settings,
            repository=store,
            text_generator_factory=AsyncMock,
            analysis_factory=lambda gateway, db_config, text_generator: fake,
        )
        app = FastAPI()
        app.include_router(
            create_catalog_router(container=container, gateways={"testdb": object()}, app=app)
        )
        r = TestClient(app).post("/catalog/analyze", json={"database": "testdb"})
        assert r.status_code == 200
        body = r.json()
        assert body["failed_tables"] == ["broken"]
        assert body["fallback_tables"] == ["orders"]
        fake.analyze.assert_awaited_once_with(table_names=None, auto_approve=False)


def _term_store(tmp_path: Path) -> CatalogFileStore:
    from trio.domain.catalog.terms import ColumnAttribute, SqlAttribute, Term

    catalog = _make_catalog()
    catalog.terms = {
        "user": Term(
            name="user",
            synonyms=["member"],
            tables=["users"],
            column_attributes=[ColumnAttribute(table="users", column="email", synonyms=["mail"])],
            sql_attributes=[SqlAttribute(name="user_count", table="users", expression="count(*)")],
        )
    }
    s = CatalogFileStore(tmp_path)
    CatalogReviewService(s).save_as_draft(catalog)
    return s


class TestTerms:
    """The review screen shows a table's terms, certifies them on approval, and edits them."""

    @pytest.fixture
    def terms_client(self, tmp_path: Path) -> tuple[TestClient, CatalogFileStore]:
        store = _term_store(tmp_path)
        app = FastAPI()
        app.include_router(create_catalog_router(container=_container(store), gateways={}))
        return TestClient(app), store

    def test_a_table_lists_the_terms_that_cover_it(self, terms_client) -> None:
        client, _ = terms_client
        (term,) = client.get("/catalog/testdb/draft/tables/users").json()["terms"]
        assert term["name"] == "user" and term["certified"] is False
        assert term["columns"] == [{"column": "email", "synonyms": ["mail"], "values": {}}]
        assert [e["name"] for e in term["expressions"]] == ["user_count"]
        assert client.get("/catalog/testdb/draft/tables/orders").json()["terms"] == []

    def test_approving_certifies_the_terms_only_when_asked(self, terms_client) -> None:
        client, store = terms_client
        client.post("/catalog/testdb/approve/users")
        assert store.load("testdb").terms["user"].certified is False
        client.post("/catalog/testdb/approve/users?certify_term=true")
        assert store.load("testdb").terms["user"].certified is True

    def test_editing_words_and_certifying_an_expression(self, terms_client) -> None:
        client, store = terms_client
        r = client.patch(
            "/catalog/testdb/terms/user",
            json={
                "synonyms": ["member", "account"],
                "columns": {"email": ["e-mail"]},
                "certify_sql": ["user_count"],
            },
        )
        assert r.status_code == 200, r.text
        term = store.load("testdb").terms["user"]
        assert term.synonyms == ["member", "account"]
        assert term.column_attributes[0].synonyms == ["e-mail"]
        assert term.sql_attributes[0].certified is True

    def test_term_edits_that_cannot_apply_are_refused(self, terms_client) -> None:
        client, store = terms_client
        assert client.patch("/catalog/testdb/terms/user", json={}).status_code == 400
        r = client.patch("/catalog/testdb/terms/ghost", json={"synonyms": ["x"]})
        assert r.status_code == 400 and "ghost" in r.json()["detail"]
        r = client.patch("/catalog/testdb/terms/user", json={"certify_sql": ["nope"]})
        assert r.status_code == 400
        assert store.load("testdb").terms["user"].sql_attributes[0].certified is False
        assert client.patch("/catalog/nope/terms/user", json={"synonyms": ["x"]}).status_code == 404

    def test_only_a_writer_edits_terms(self, tmp_path: Path) -> None:
        store = _term_store(tmp_path)
        app = FastAPI()
        app.include_router(
            create_catalog_router(
                container=_container(store), gateways={}, auth_manager=_auth_manager()
            )
        )
        client = TestClient(app)
        body = {"synonyms": ["x"]}
        assert client.patch("/catalog/testdb/terms/user", json=body).status_code == 401
        r = client.patch("/catalog/testdb/terms/user", json=body, headers=_h("reader-key"))
        assert r.status_code == 403
        r = client.patch("/catalog/testdb/terms/user", json=body, headers=_h("writer-key"))
        assert r.status_code == 200


class TestAudit:
    """Each catalog change that takes effect leaves one event, naming what changed but no text."""

    @pytest.fixture
    def audited(self, tmp_path: Path):
        from dataclasses import replace

        class Sink:
            def __init__(self) -> None:
                self.events: list = []

            def record(self, event) -> None:
                self.events.append(event)

        sink = Sink()
        store = _term_store(tmp_path)
        container = replace(_container(store), audit=sink)
        app = FastAPI()
        app.include_router(
            create_catalog_router(container=container, gateways={}, auth_manager=_auth_manager())
        )
        return TestClient(app), sink

    def test_edits_and_approvals_are_recorded_by_name(self, audited) -> None:
        client, sink = audited
        h = _h("writer-key")
        client.patch(
            "/catalog/testdb/draft/tables/users/columns/email",
            json={"semantic_type": "personal_email", "description": {"en": "SECRET TEXT"}},
            headers=h,
        )
        client.patch("/catalog/testdb/terms/user", json={"certify_sql": ["user_count"]}, headers=h)
        client.post("/catalog/testdb/approve/users?certify_term=true", headers=h)
        client.post("/catalog/testdb/approve", json={}, headers=h)

        changes = [(e.action, e.table, e.change) for e in sink.events]
        assert changes == [
            ("update", "users", "edit_column email description,semantic_type"),
            ("update", "users", "edit_term user certify_sql certify_sql=user_count"),
            ("update", "users", "approve_table certify_term"),
            ("update", "", "approve_catalog"),
        ]
        first = sink.events[0].as_dict()
        assert first["event"] == "catalog_change" and first["actor"] == "writer"
        assert first["database"] == "testdb"
        assert "SECRET" not in repr([e.as_dict() for e in sink.events])
        assert (
            sink.events[2].summary()
            == "writer catalog approve_table certify_term testdb.users status=200"
        )

    def test_a_delete_is_recorded(self, audited) -> None:
        client, sink = audited
        assert client.delete("/catalog/testdb", headers=_h("admin-key")).status_code == 200
        assert [(e.action, e.actor, e.change) for e in sink.events] == [
            ("delete", "admin", "delete_catalog")
        ]

    def test_nothing_is_recorded_when_nothing_changed(self, audited) -> None:
        client, sink = audited
        body = {"synonyms": ["x"]}
        assert (
            client.patch(
                "/catalog/testdb/terms/user", json=body, headers=_h("reader-key")
            ).status_code
            == 403
        )
        assert (
            client.patch(
                "/catalog/testdb/terms/ghost", json=body, headers=_h("writer-key")
            ).status_code
            == 400
        )
        assert (
            client.post("/catalog/testdb/approve/ghost", headers=_h("writer-key")).status_code
            == 400
        )
        assert sink.events == []
        client.post("/catalog/testdb/approve", json={}, headers=_h("writer-key"))
        sink.events.clear()
        r = client.patch(
            "/catalog/testdb/draft/tables/users", json={"tags": ["x"]}, headers=_h("writer-key")
        )
        assert r.status_code == 409  # approved now: not a draft
        assert sink.events == []


class TestReviewerAids:
    """What the review screen needs: how far relationships are borne out, and certifying all terms."""

    def test_a_relationship_says_whether_the_data_bears_it_out(self, client: TestClient) -> None:
        (rel,) = client.get("/catalog/testdb/draft/tables/users").json()["relationships"]
        assert rel["verified"] is None

    def test_certifying_every_term_at_once(self, tmp_path: Path) -> None:
        from dataclasses import replace

        events: list = []
        sink = type("Sink", (), {"record": lambda self, e: events.append(e)})()
        store = _term_store(tmp_path)
        app = FastAPI()
        app.include_router(
            create_catalog_router(
                container=replace(_container(store), audit=sink),
                gateways={},
                auth_manager=_auth_manager(),
            )
        )
        client = TestClient(app)
        assert (
            client.get("/catalog/testdb/draft", headers=_h("reader-key")).json()[
                "uncertified_terms"
            ]
            == 1
        )
        assert (
            client.post("/catalog/testdb/terms/certify", headers=_h("reader-key")).status_code
            == 403
        )
        r = client.post("/catalog/testdb/terms/certify", headers=_h("writer-key"))
        assert r.json() == {"database": "testdb", "certified": 1}
        assert store.load("testdb").terms["user"].certified is True
        assert [e.change for e in events] == ["certify_terms 1"]
        # Nothing left to certify: no change, no event.
        assert (
            client.post("/catalog/testdb/terms/certify", headers=_h("writer-key")).json()[
                "certified"
            ]
            == 0
        )
        assert len(events) == 1
        assert (
            client.post("/catalog/nope/terms/certify", headers=_h("writer-key")).status_code == 404
        )


class TestValuesKeptFromRestrictedCallers:
    """Stored values follow the same rule as the OLAP term tools.

    A row-ruled table's samples may come from other tenants' rows, and a masked
    semantic type's samples are the very values the mask hides.
    """

    @pytest.fixture
    def keyed(self, tmp_path: Path) -> tuple[TestClient, CatalogFileStore]:
        from trio.domain.catalog.terms import ColumnAttribute, Term

        catalog = _make_catalog()
        users = catalog.tables["users"]
        users.columns.append(
            ColumnCatalogEntry(name="segment", data_type="varchar", sample_values=["a", "b"])
        )
        catalog.tables["orders"].columns[0].sample_values = [1, 2]
        catalog.terms = {
            "user": Term(
                name="user",
                tables=["users"],
                column_attributes=[
                    ColumnAttribute(
                        table="users", column="segment", values={"b": ["other tenant"]}
                    ),
                    ColumnAttribute(table="users", column="email", values={"a@x.com": ["Ann"]}),
                ],
            )
        }
        store = CatalogFileStore(tmp_path)
        CatalogReviewService(store).save_as_draft(catalog)

        settings = Settings()
        settings.settings.masking.enabled = True
        settings.settings.masking.rules = {"email": "redact"}
        settings.settings.masking.exempt_roles = ["dpo"]
        auth = AuthManager(
            AuthConfig(
                enabled=True,
                api_keys=[
                    ApiKeyConfig(key="reader", name="reader", permissions=["read"]),
                    ApiKeyConfig(key="dpo", name="dpo", roles=["dpo"], permissions=["read"]),
                    ApiKeyConfig(
                        key="tenant",
                        name="tenant",
                        tenant="a",
                        roles=["dpo"],  # unmasked, so only the row rule withholds
                        permissions=["read"],
                        row_filters={"users": [{"column": "segment", "value": "a"}]},
                    ),
                ],
            )
        )
        app = FastAPI()
        app.include_router(
            create_catalog_router(
                container=_container(store, settings), gateways={}, app=app, auth_manager=auth
            )
        )
        return TestClient(app), store

    @staticmethod
    def _seen(client: TestClient, key: str, table: str) -> tuple[dict, dict]:
        body = client.get(f"/catalog/testdb/draft/tables/{table}", headers=_h(key)).json()
        samples = {c["name"]: c["sample_values"] for c in body["columns"]}
        values = {a["column"]: a["values"] for t in body["terms"] for a in t["columns"]}
        return samples, values

    def test_a_row_ruled_table_shows_no_values_and_other_tables_do(self, keyed) -> None:
        client, _ = keyed
        samples, values = self._seen(client, "tenant", "users")
        assert samples == {"id": [], "email": [], "segment": []}
        assert values == {"segment": {}, "email": {}}
        assert self._seen(client, "tenant", "orders")[0]["id"] == [1, 2]

    def test_a_masked_type_shows_no_values_and_the_rest_does(self, keyed) -> None:
        client, _ = keyed
        samples, values = self._seen(client, "reader", "users")
        assert samples["email"] == [] and samples["segment"] == ["a", "b"]
        assert values == {"segment": {"b": ["other tenant"]}, "email": {}}
        assert self._seen(client, "dpo", "users")[0]["email"] == ["a@x.com"]

    def test_the_export_withholds_the_same_and_the_store_keeps_them(self, keyed) -> None:
        client, store = keyed
        text = client.get("/catalog/testdb/export?format=json", headers=_h("tenant")).text
        assert "a@x.com" not in text and '"b"' not in text
        assert "a@x.com" in client.get("/catalog/testdb/export", headers=_h("dpo")).text
        assert store.load("testdb").tables["users"].get_column("segment").sample_values == [
            "a",
            "b",
        ]

    def test_the_ontology_holds_everything_but_what_the_caller_may_not_see(self, keyed) -> None:
        client, _ = keyed
        body = client.get("/catalog/testdb/ontology", headers=_h("tenant")).json()
        users = {c["name"]: c for c in body["tables"]["users"]["columns"]}
        assert users["segment"]["sample_values"] == []
        assert body["terms"]["user"]["column_attributes"][0]["values"] == {}
        assert body["tables"]["orders"]["columns"][0]["sample_values"] == [1, 2]
        assert body["tables"]["users"]["relationships"][0]["target_table"] == "orders"
        # Personal by the analysis rule; masked only where the caller's roles mask it.
        assert users["email"]["personal"] is True and users["segment"]["personal"] is False
        assert users["email"]["masked"] is False  # the tenant key is exempt
        reader = client.get("/catalog/testdb/ontology", headers=_h("reader")).json()
        email = next(c for c in reader["tables"]["users"]["columns"] if c["name"] == "email")
        assert email["masked"] is True and email["sample_values"] == []
        assert client.get("/catalog/nope/ontology", headers=_h("reader")).status_code == 404
