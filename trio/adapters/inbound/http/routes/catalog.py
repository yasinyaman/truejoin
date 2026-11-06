"""Catalog REST API endpoints.

Provides REST endpoints for catalog operations:
- Analyze database and generate catalog
- List available catalogs
- Get catalog info
- Export catalog
- Enrich OpenAPI spec
"""

import logging
from collections.abc import Callable, Mapping
from typing import Annotated, Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import Response
from pydantic import BaseModel, Field

from trio.adapters.inbound.http.auth import AuthManager, Permission, caller_of
from trio.adapters.inbound.http.olap import masked_types
from trio.adapters.inbound.http.request_context import request_id_of
from trio.application.catalog.openapi_enrichment import OpenAPIEnricher, withheld_from_spec
from trio.application.container import Container
from trio.domain.catalog.catalog import CatalogStatus, DatabaseCatalog, structural_catalog
from trio.domain.catalog.catalog_naming import CATALOG_NAME_PATTERN
from trio.domain.catalog.terms import Term
from trio.domain.gateway.errors import (
    AnalysisError,
    DatabaseNotConfiguredError,
    LLMError,
    UnsupportedExportFormatError,
)
from trio.domain.gateway.samples import looks_personal
from trio.domain.gateway.schema import DatabaseSchema
from trio.domain.governance.audit import AuditAction, AuditEvent
from trio.ports.database import DatabaseGateway

logger = logging.getLogger(__name__)

# Path parameter type for catalog names: rejects anything that is not a plain
# identifier (e.g. "..", "a/b", "_index") with a 422 before it reaches the store.
CatalogName = Annotated[
    str,
    PathParam(
        pattern=CATALOG_NAME_PATTERN,
        description="Catalog (database) name: letters, digits, '_' or '-'.",
    ),
]


# --- Request/Response models ---


class AnalyzeRequest(BaseModel):
    """Request body for catalog analysis."""

    database: str = Field(..., description="Database config name")
    tables: list[str] | None = Field(default=None, description="Specific tables to analyze")
    lang: str | None = Field(default=None, description="Language override")
    auto_approve: bool = Field(
        default=False, description="Auto-approve catalog after analysis (skip review)"
    )


class AnalyzeResponse(BaseModel):
    """Response from catalog analysis."""

    database: str
    table_count: int
    languages: list[str]
    status: str = "draft"
    failed_tables: list[str] = Field(
        default_factory=list, description="Tables left out because their schema could not be read"
    )
    fallback_tables: list[str] = Field(
        default_factory=list, description="Tables catalogued without LLM descriptions"
    )


class CatalogListEntry(BaseModel):
    """Summary of a catalog in the list."""

    database_name: str
    database_type: str = "postgresql"
    table_count: int = 0
    languages: list[str] = Field(default_factory=list)
    version: str = "1.0.0"
    status: str = "draft"


class CatalogListResponse(BaseModel):
    """Response for catalog list."""

    catalogs: list[CatalogListEntry]
    total: int
    connected: list[str] = Field(
        default_factory=list, description="Databases connected now: the ones that can be analyzed"
    )


class SuggestRequest(BaseModel):
    """Request body for suggesting measures from the query history and the views."""

    limit: int = Field(default=20, ge=1, le=100, description="Most drafts to add in one run")
    statements: int = Field(
        default=500, ge=1, le=10_000, description="Most statements to read from the history"
    )
    use_history: bool = Field(default=True, description="Read the statement history")
    use_views: bool = Field(default=True, description="Read the view definitions")


class CatalogInfoResponse(BaseModel):
    """Detailed catalog info."""

    database_name: str
    database_type: str
    table_count: int
    languages: list[str]
    tables: list[dict[str, Any]]
    llm_provider: str | None = None
    llm_model: str | None = None


class ExportRequest(BaseModel):
    """Request for catalog export."""

    format: str = Field(default="json", description="Export format: json, yaml, markdown")
    lang: str = Field(default="en", description="Language for export")


class TableEditRequest(BaseModel):
    """Partial update for a table's editable fields."""

    description: dict[str, str] | None = Field(
        default=None,
        description="Localized description updates, e.g. {'en': 'New desc'}",
    )
    human_name: dict[str, str] | None = Field(
        default=None,
        description="Localized human name updates",
    )
    tags: list[str] | None = Field(
        default=None,
        description="Replace tags list",
    )
    relationships: list[dict[str, Any]] | None = Field(
        default=None,
        description="Replace relationships list",
    )


class ColumnEditRequest(BaseModel):
    """Partial update for a column's editable fields."""

    description: dict[str, str] | None = Field(
        default=None,
        description="Localized description updates",
    )
    semantic_type: str | None = Field(
        default=None,
        description="Semantic type override",
    )
    tags: list[str] | None = Field(
        default=None,
        description="Replace tags list",
    )


class ApproveRequest(BaseModel):
    """Request to approve tables or entire catalog."""

    tables: list[str] | None = Field(
        default=None,
        description="Specific table names to approve. If None, approve all.",
    )


class DraftInfoResponse(BaseModel):
    """Draft catalog info with per-table review status."""

    database_name: str
    database_type: str
    status: str
    table_count: int
    languages: list[str]
    review_summary: dict[str, int]
    tables: list[dict[str, Any]]
    uncertified_terms: int = 0


class TableReviewDetail(BaseModel):
    """Detailed table info for review."""

    table_name: str
    human_name: dict[str, str]
    description: dict[str, str]
    review_status: str
    tags: list[str]
    column_count: int
    row_count: int | None = None
    columns: list[dict[str, Any]]
    relationships: list[dict[str, Any]]
    user_overrides: dict[str, Any] = Field(default_factory=dict)
    terms: list[dict[str, Any]] = Field(
        default_factory=list,
        description="The business terms that cover this table, as a reviewer certifies them",
    )


class TermEditRequest(BaseModel):
    """A reviewer's edits to one business term."""

    synonyms: list[str] | None = Field(default=None, description="Replace the term's synonyms")
    columns: dict[str, list[str]] | None = Field(
        default=None, description="Replace the words for columns: {column: [words]}"
    )
    certify_sql: list[str] | None = Field(
        default=None, description="Names of the term's draft expressions to certify as measures"
    )


def _term_detail(
    term: Term, table_name: str, withheld: Callable[[str, str], bool] = lambda _t, _c: False
) -> dict[str, Any]:
    """What a reviewer of ``table_name`` needs to see of ``term`` before certifying it."""
    return {
        "name": term.name,
        "description": term.description.texts,
        "synonyms": term.synonyms,
        "certified": term.certified,
        "tables": term.tables,
        "columns": [
            {
                "column": a.column,
                "synonyms": a.synonyms,
                "values": {} if withheld(a.table, a.column) else a.values,
            }
            for a in term.column_attributes
            if a.table == table_name
        ],
        "expressions": [
            {
                "name": s.name,
                "expression": s.expression,
                "description": s.description.texts,
                "certified": s.certified,
            }
            for s in term.sql_attributes
            if s.table == table_name
        ],
    }


def _refresh_openapi_enrichment(
    app: FastAPI,
    container: Container,
    gateways: dict[str, DatabaseGateway],
    schemas: Mapping[str, DatabaseSchema] | None = None,
) -> None:
    """Rebuild OpenAPI enrichment after catalog changes.

    Clears the cached OpenAPI schema and installs a new generator.

    A database with an approved catalog is enriched from it, descriptions and
    semantic types included. One without falls back to what the engine itself
    reported — types, keys and relationships — so a consumer can see that
    `musteri_id` points at another table without waiting for an LLM pass over
    every table and a reviewer to approve it. Structure is mechanical; only
    the words need a model.
    """
    from fastapi.openapi.utils import get_openapi

    store = container.repository
    config = container.settings
    if not config.settings.catalog.auto_enrich_openapi:
        logger.debug("OpenAPI enrichment disabled in config")
        return

    enrichment_lang = config.settings.catalog.openapi_enrichment_lang
    enrichers = []
    structural = 0
    for db_name in gateways:
        catalog = store.load(db_name)
        if catalog is None or catalog.status != CatalogStatus.approved:
            # A draft's words have not been reviewed, so none of them are
            # published — but its structure was never the model's to say.
            schema = (schemas or {}).get(db_name)
            if schema is None:
                logger.debug(f"OpenAPI enrichment: nothing to enrich '{db_name}' with")
                continue
            catalog = structural_catalog(db_name, schema)
            structural += 1
            logger.debug(f"OpenAPI enrichment: structure only for '{db_name}'")
        enrichers.append(
            OpenAPIEnricher(
                catalog,
                lang=enrichment_lang,
                include_examples=config.settings.catalog.openapi_include_examples,
                **withheld_from_spec(config.settings),
            )
        )

    def enriched_openapi() -> dict[str, Any]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title, version=app.version, description=app.description, routes=app.routes
        )
        for enricher in enrichers:  # each merges into one top-level x-llm-context
            schema = enricher.enrich(schema)
        app.openapi_schema = schema
        return app.openapi_schema

    # Always replace both the generator and the cache: a stale closure would
    # keep serving descriptions of catalogs that were deleted or re-opened.
    app.openapi = enriched_openapi  # type: ignore[method-assign]
    app.openapi_schema = None
    enriched_openapi()
    approved = len(enrichers) - structural
    logger.info(
        f"OpenAPI enrichment refreshed: {approved} approved catalog(s), "
        f"{structural} from schema structure alone"
    )


def create_catalog_router(  # noqa: C901, PLR0915
    *,
    container: Container,
    gateways: dict[str, DatabaseGateway] | None = None,
    app: FastAPI | None = None,
    auth_manager: AuthManager | None = None,
) -> APIRouter:
    """Create the catalog API router.

    Permissions (enforced when `auth_manager` is enabled):
        read   - list/info/export/draft views
        create - POST /analyze (writes a new catalog, spends LLM budget)
        update - draft edits and approvals
        delete - DELETE /{db_name}

    Args:
        container: The wired application services (repository, review, export,
            analysis).
        gateways: Connected database gateways by name (for live analysis).
        app: FastAPI app instance (for OpenAPI enrichment refresh)
        auth_manager: Optional auth manager; when enabled every endpoint
            requires an API key with the matching permission.

    Returns:
        FastAPI APIRouter with catalog endpoints
    """
    store = container.repository
    review = container.review
    adapters = gateways
    router = APIRouter(prefix="/catalog", tags=["Catalog"])

    def get_auth_deps(permission: Permission) -> list[Any]:
        if auth_manager and auth_manager.enabled:
            return [Depends(auth_manager.require(permission))]
        return []

    def withheld_values(http: Request, catalog: DatabaseCatalog) -> Callable[[str, str], bool]:
        """Whether a column's stored values are kept from this caller.

        The same rule as the OLAP term tools: every column of a table the
        caller has row rules on (its samples may come from other tenants'
        rows), and every column whose semantic type is masked for them.
        """
        caller = caller_of(http)
        masked = masked_types(container.settings.settings.masking, caller.roles if caller else [])

        def withheld(table_name: str, column_name: str) -> bool:
            if caller is not None and caller.row_policy.conditions_for(table_name):
                return True
            table = catalog.get_table(table_name)
            column = table.get_column(column_name) if table else None
            return column is not None and column.semantic_type in masked

        return withheld

    def visible_to(http: Request, catalog: DatabaseCatalog) -> DatabaseCatalog:
        """A copy of ``catalog`` without the stored values this caller may not see."""
        catalog = catalog.model_copy(deep=True)  # stripped for this caller, never saved
        withheld = withheld_values(http, catalog)
        for tname, table in catalog.tables.items():
            for col in table.columns:
                if withheld(tname, col.name):
                    col.sample_values = []
        for term in catalog.terms.values():
            for attribute in term.column_attributes:
                if withheld(attribute.table, attribute.column):
                    attribute.values = {}
        return catalog

    def record(http: Request, action: AuditAction, db_name: str, table: str, change: str) -> None:
        """One audit event per catalog change that took effect (the sink never raises)."""
        caller = caller_of(http)
        container.audit.record(
            AuditEvent(
                action=action,
                database=db_name,
                table=table,
                actor=caller.name if caller else None,
                tenant=caller.tenant if caller else None,
                roles=tuple(caller.roles) if caller else (),
                request_id=request_id_of(http),
                change=change,
            )
        )

    @router.get(
        "",
        dependencies=get_auth_deps(Permission.READ),
        response_model=CatalogListResponse,
        summary="List available catalogs",
    )
    async def list_catalogs() -> CatalogListResponse:
        """List all available catalogs."""
        catalog_names = store.list_catalogs()
        index = store.get_index()

        entries = []
        for name in catalog_names:
            idx_entry = index.catalogs.get(name)
            if idx_entry:
                entries.append(
                    CatalogListEntry(
                        database_name=idx_entry.database_name,
                        database_type=idx_entry.database_type,
                        table_count=idx_entry.table_count,
                        languages=idx_entry.languages,
                        version=idx_entry.version,
                        status=getattr(idx_entry, "status", "draft"),
                    )
                )
            else:
                entries.append(CatalogListEntry(database_name=name))

        return CatalogListResponse(
            catalogs=entries, total=len(entries), connected=sorted(adapters or {})
        )

    @router.get(
        "/{db_name}",
        dependencies=get_auth_deps(Permission.READ),
        response_model=CatalogInfoResponse,
        summary="Get catalog info",
    )
    async def get_catalog_info(
        db_name: CatalogName,
        lang: str = Query(default="en", description="Language for descriptions"),
    ) -> CatalogInfoResponse:
        """Get detailed information about a specific catalog."""
        catalog = store.load(db_name)
        if not catalog:
            raise HTTPException(status_code=404, detail=f"Catalog not found: {db_name}")

        tables_info = []
        for tname, table in catalog.tables.items():
            human = table.human_name.get(lang) or tname
            desc = table.description.get(lang) or ""
            tables_info.append(
                {
                    "table_name": tname,
                    "human_name": human,
                    "description": desc,
                    "column_count": len(table.columns),
                    "row_count": table.row_count,
                    "tags": table.tags,
                }
            )

        return CatalogInfoResponse(
            database_name=catalog.database_name,
            database_type=catalog.database_type,
            table_count=catalog.table_count,
            languages=catalog.languages,
            tables=tables_info,
            llm_provider=catalog.llm_provider,
            llm_model=catalog.llm_model,
        )

    @router.get(
        "/{db_name}/export",
        dependencies=get_auth_deps(Permission.READ),
        summary="Export catalog",
    )
    async def export_catalog(
        db_name: CatalogName,
        http: Request,
        format: str = Query(default="json", description="Export format"),
        lang: str = Query(default="en", description="Language"),
    ) -> Response:
        """Export a catalog in the specified format."""
        catalog = store.load(db_name)
        if not catalog:
            raise HTTPException(status_code=404, detail=f"Catalog not found: {db_name}")
        try:
            content = container.export.render(visible_to(http, catalog), format, lang=lang)
        except UnsupportedExportFormatError as e:
            raise HTTPException(status_code=400, detail=e.message) from e

        return Response(
            content=content,
            media_type=container.export.media_type(format),
            headers={"Content-Disposition": f'attachment; filename="{db_name}_catalog.{format}"'},
        )

    @router.post(
        "/analyze",
        dependencies=get_auth_deps(Permission.CREATE),
        response_model=AnalyzeResponse,
        summary="Analyze database and generate catalog",
    )
    async def analyze_database(
        request: AnalyzeRequest,
        http: Request,
    ) -> AnalyzeResponse:
        """Run database analysis and catalog generation.

        The analysis runs synchronously within the request (one LLM call per
        table); the response is returned when the catalog has been written.
        """
        if not adapters:
            raise HTTPException(
                status_code=503,
                detail="Analysis not available - no database gateways are connected",
            )

        if request.database not in adapters:
            raise HTTPException(
                status_code=404,
                detail=f"Database not connected: {request.database}",
            )

        adapter = adapters[request.database]
        try:
            async with container.open_analysis(request.database, gateway=adapter) as analysis:
                catalog = await analysis.analyze(
                    table_names=request.tables,
                    auto_approve=request.auto_approve,
                )
                report = analysis.report

            # A re-analysis re-opens the catalog as a draft (or auto-approves it):
            # either way the published OpenAPI context must follow.
            if app and adapters:
                _refresh_openapi_enrichment(app, container, adapters)

            change = "analyze" + (" auto_approve" if request.auto_approve else "")
            record(http, "create", catalog.database_name, ",".join(request.tables or ()), change)
            return AnalyzeResponse(
                database=catalog.database_name,
                table_count=catalog.table_count,
                languages=catalog.languages,
                status=catalog.status.value,
                failed_tables=report.failed_tables if report else [],
                fallback_tables=report.fallback_tables if report else [],
            )
        except DatabaseNotConfiguredError as e:
            raise HTTPException(status_code=404, detail=e.message) from e
        except (AnalysisError, LLMError) as e:
            logger.error(f"Analysis failed for {request.database}: {e}")
            raise HTTPException(
                status_code=422,
                detail=str(e),
            ) from e
        except Exception as e:
            # Never echo unexpected exception text (driver/SDK internals) to
            # the client; the traceback is logged server-side.
            logger.exception(f"Analysis failed for {request.database}: {e}")
            raise HTTPException(status_code=500, detail="Analysis failed") from e

    @router.post(
        "/{db_name}/suggest",
        dependencies=get_auth_deps(Permission.UPDATE),
        summary="Suggest measures from the query history and the views",
    )
    async def suggest_measures(
        db_name: CatalogName, request: SuggestRequest, http: Request
    ) -> dict[str, Any]:
        """What `trio catalog suggest` does: store recurring expressions as draft measures.

        Runs within the request (one model call to name the drafts). A draft
        is not a measure until it is certified on its term.
        """
        if not store.load(db_name):
            raise HTTPException(status_code=404, detail=f"Catalog not found: {db_name}")
        try:
            async with container.open_mining(db_name) as mining:
                report = await mining.suggest(
                    limit=request.limit,
                    statements=request.statements,
                    use_history=request.use_history,
                    use_views=request.use_views,
                )
        except DatabaseNotConfiguredError as e:
            raise HTTPException(status_code=404, detail=e.message) from e
        except LLMError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        except Exception as e:
            logger.exception(f"Suggesting measures failed for {db_name}: {e}")
            raise HTTPException(status_code=500, detail="Suggesting measures failed") from e
        if report.added:
            record(
                http,
                "update",
                db_name,
                ",".join(sorted({a.table for a in report.added})),
                f"suggest_sql {','.join(a.name for a in report.added)}",
            )
        return {
            "database": db_name,
            "statements": report.statements,
            "views": report.views,
            "unavailable": report.unavailable,
            "without_term": report.without_term,
            "added": [
                a.model_dump(mode="json", include={"name", "table", "expression", "description"})
                for a in report.added
            ],
        }

    @router.delete(
        "/{db_name}",
        dependencies=get_auth_deps(Permission.DELETE),
        summary="Delete a catalog",
    )
    async def delete_catalog(db_name: CatalogName, http: Request) -> dict[str, Any]:
        """Delete a stored catalog (its descriptions leave the OpenAPI spec too)."""
        if store.delete(db_name):
            record(http, "delete", db_name, "", "delete_catalog")
            if app and adapters:
                _refresh_openapi_enrichment(app, container, adapters)
            return {"deleted": True, "database": db_name}
        raise HTTPException(status_code=404, detail=f"Catalog not found: {db_name}")

    # --- Draft / Review endpoints ---

    @router.get(
        "/{db_name}/ontology",
        dependencies=get_auth_deps(Permission.READ),
        summary="The whole catalog: tables, columns, keys, relationships, terms and measures",
    )
    async def get_ontology(db_name: CatalogName, http: Request) -> dict[str, Any]:
        """Everything the catalog holds, in any status, for reading.

        Stored values follow the same rule as the draft table detail. Each
        column also says whether it looks personal (the rule analysis keeps
        samples by) and whether its semantic type is masked for this caller.
        """
        catalog = store.load(db_name)
        if not catalog:
            raise HTTPException(status_code=404, detail=f"Catalog not found: {db_name}")
        caller = caller_of(http)
        settings = container.settings.settings
        masked = masked_types(settings.masking, caller.roles if caller else [])
        body = visible_to(http, catalog).model_dump(mode="json")
        for table in body["tables"].values():
            for col in table["columns"]:
                col["personal"] = looks_personal(
                    col["name"],
                    col["semantic_type"],
                    col["tags"],
                    settings.analysis.pii_column_patterns,
                )
                col["masked"] = col["semantic_type"] in masked
        return body

    @router.get(
        "/{db_name}/draft",
        dependencies=get_auth_deps(Permission.READ),
        response_model=DraftInfoResponse,
        summary="Get draft catalog with review status",
    )
    async def get_draft_info(
        db_name: CatalogName,
        lang: str = Query(default="en", description="Language for descriptions"),
    ) -> DraftInfoResponse:
        """Get draft catalog overview with per-table review status."""
        catalog = store.load(db_name)
        if not catalog:
            raise HTTPException(status_code=404, detail=f"Catalog not found: {db_name}")

        tables_info = []
        for tname, table in catalog.tables.items():
            tables_info.append(
                {
                    "table_name": tname,
                    "human_name": table.human_name.get(lang) or tname,
                    "description": table.description.get(lang) or "",
                    "review_status": table.review_status.value,
                    "column_count": len(table.columns),
                    "row_count": table.row_count,
                    "tags": table.tags,
                    "has_overrides": bool(table.user_overrides),
                }
            )

        return DraftInfoResponse(
            database_name=catalog.database_name,
            database_type=catalog.database_type,
            status=catalog.status.value,
            table_count=catalog.table_count,
            languages=catalog.languages,
            review_summary=catalog.review_summary,
            tables=tables_info,
            uncertified_terms=sum(not term.certified for term in catalog.terms.values()),
        )

    @router.get(
        "/{db_name}/draft/tables/{table_name}",
        dependencies=get_auth_deps(Permission.READ),
        response_model=TableReviewDetail,
        summary="Get single table detail for review",
    )
    async def get_draft_table(
        db_name: CatalogName,
        table_name: str,
        http: Request,
        lang: str = Query(default="en", description="Language"),
    ) -> TableReviewDetail:
        """Get detailed table info for review including columns and relationships."""
        catalog = store.load(db_name)
        if not catalog:
            raise HTTPException(status_code=404, detail=f"Catalog not found: {db_name}")

        table = catalog.get_table(table_name)
        if not table:
            raise HTTPException(
                status_code=404,
                detail=f"Table not found: {table_name} in {db_name}",
            )
        withheld = withheld_values(http, catalog)

        columns_info = []
        for col in table.columns:
            columns_info.append(
                {
                    "name": col.name,
                    "data_type": col.data_type,
                    "description": col.description.texts,
                    "semantic_type": col.semantic_type,
                    "nullable": col.nullable,
                    "is_primary_key": col.is_primary_key,
                    "is_foreign_key": col.is_foreign_key,
                    "references": col.references,
                    "tags": col.tags,
                    "sample_values": [] if withheld(table_name, col.name) else col.sample_values,
                    "has_overrides": bool(col.user_overrides),
                }
            )

        relationships_info = []
        for rel in table.relationships:
            relationships_info.append(
                {
                    "source_column": rel.source_column,
                    "target_table": rel.target_table,
                    "target_column": rel.target_column,
                    "relationship_type": rel.relationship_type,
                    "description": rel.description.texts,
                    # True: the data bears it out; False: the data contradicts it;
                    # None: not checked. Only a verified one is used for joins.
                    "verified": rel.verified,
                }
            )

        return TableReviewDetail(
            table_name=table.table_name,
            human_name=table.human_name.texts,
            description=table.description.texts,
            review_status=table.review_status.value,
            tags=table.tags,
            column_count=len(table.columns),
            row_count=table.row_count,
            columns=columns_info,
            relationships=relationships_info,
            user_overrides=table.user_overrides,
            terms=[
                _term_detail(term, table_name, withheld)
                for term in catalog.terms.values()
                if table_name in term.tables
            ],
        )

    @router.patch(
        "/{db_name}/draft/tables/{table_name}",
        dependencies=get_auth_deps(Permission.UPDATE),
        summary="Edit table fields in draft",
    )
    async def edit_draft_table(
        db_name: CatalogName,
        table_name: str,
        request: TableEditRequest,
        http: Request,
        lang: str = Query(default="en", description="Default language for string values"),
    ) -> dict[str, Any]:
        """Edit table-level fields. The table review_status becomes 'modified'."""
        catalog = store.load(db_name)
        if not catalog:
            raise HTTPException(status_code=404, detail=f"Catalog not found: {db_name}")

        if catalog.status.value != "draft":
            raise HTTPException(
                status_code=409,
                detail=f"Catalog is not in draft status: {db_name}",
            )

        updates = request.model_dump(exclude_none=True)
        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")

        try:
            table = review.update_table_fields(
                db_name=db_name,
                table_name=table_name,
                updates=updates,
                lang=lang,
            )
            record(http, "update", db_name, table_name, f"edit_table {','.join(sorted(updates))}")
            return {
                "table_name": table.table_name,
                "review_status": table.review_status.value,
                "updated_fields": list(updates.keys()),
            }
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.patch(
        "/{db_name}/draft/tables/{table_name}/columns/{col_name}",
        dependencies=get_auth_deps(Permission.UPDATE),
        summary="Edit column fields in draft",
    )
    async def edit_draft_column(
        db_name: CatalogName,
        table_name: str,
        col_name: str,
        request: ColumnEditRequest,
        http: Request,
        lang: str = Query(default="en", description="Default language"),
    ) -> dict[str, Any]:
        """Edit column-level fields. The parent table review_status becomes 'modified'."""
        catalog = store.load(db_name)
        if not catalog:
            raise HTTPException(status_code=404, detail=f"Catalog not found: {db_name}")

        if catalog.status.value != "draft":
            raise HTTPException(
                status_code=409,
                detail=f"Catalog is not in draft status: {db_name}",
            )

        updates = request.model_dump(exclude_none=True)
        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")

        try:
            column = review.update_column_fields(
                db_name=db_name,
                table_name=table_name,
                column_name=col_name,
                updates=updates,
                lang=lang,
            )
            fields = ",".join(sorted(updates))
            record(http, "update", db_name, table_name, f"edit_column {col_name} {fields}")
            return {
                "column_name": column.name,
                "semantic_type": column.semantic_type,
                "updated_fields": list(updates.keys()),
            }
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e)) from e

    @router.post(
        "/{db_name}/approve",
        dependencies=get_auth_deps(Permission.UPDATE),
        summary="Approve catalog (all or specific tables)",
    )
    async def approve_catalog_endpoint(
        db_name: CatalogName,
        http: Request,
        request: ApproveRequest = ApproveRequest(),
    ) -> dict[str, Any]:
        """Approve all tables or specific tables in a draft catalog.

        If request.tables is None, approves all pending tables and
        transitions the catalog to 'approved' status.
        """
        catalog = store.load(db_name)
        if not catalog:
            raise HTTPException(status_code=404, detail=f"Catalog not found: {db_name}")

        if request.tables:
            for tname in request.tables:
                try:
                    review.approve_table(db_name, tname)
                except Exception as e:
                    raise HTTPException(status_code=400, detail=str(e)) from e
                # Each approval is saved as it happens, so each is recorded then too.
                record(http, "update", db_name, tname, "approve_table")

            catalog = store.load_or_raise(db_name)
            if catalog.all_tables_approved:
                catalog = review.approve_catalog(db_name)
        else:
            catalog = review.approve_catalog(db_name)
            record(http, "update", db_name, "", "approve_catalog")

        # Refresh OpenAPI enrichment after approval
        if catalog.status.value == "approved" and app and adapters:
            _refresh_openapi_enrichment(app, container, adapters)

        return {
            "database": db_name,
            "status": catalog.status.value,
            "review_summary": catalog.review_summary,
        }

    @router.post(
        "/{db_name}/approve/{table_name}",
        dependencies=get_auth_deps(Permission.UPDATE),
        summary="Approve a single table",
    )
    async def approve_single_table(
        db_name: CatalogName,
        table_name: str,
        http: Request,
        certify_term: bool = Query(
            default=False,
            description="Also certify the terms covering this table: only for a client that "
            "showed the reviewer those terms (the draft table detail lists them)",
        ),
    ) -> dict[str, Any]:
        """Approve a single table in the draft catalog."""
        try:
            catalog = review.approve_table(db_name, table_name, certify_term=certify_term)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        change = "approve_table" + (" certify_term" if certify_term else "")
        record(http, "update", db_name, table_name, change)

        if catalog.all_tables_approved:
            catalog = review.approve_catalog(db_name)
            if app and adapters:
                _refresh_openapi_enrichment(app, container, adapters)

        return {
            "database": db_name,
            "table": table_name,
            "catalog_status": catalog.status.value,
            "review_summary": catalog.review_summary,
        }

    @router.post(
        "/{db_name}/terms/certify",
        dependencies=get_auth_deps(Permission.UPDATE),
        summary="Certify every business term not yet certified",
    )
    async def certify_all_terms(db_name: CatalogName, http: Request) -> dict[str, Any]:
        """Certify every term at once, unseen: `catalog approve --certify-terms`.

        Approving a table one by one certifies the terms the reviewer was shown;
        this is for the reviewer who says, in so many words, that all are right.
        """
        if not store.load(db_name):
            raise HTTPException(status_code=404, detail=f"Catalog not found: {db_name}")
        certified = review.certify_terms(db_name)
        if certified:
            record(http, "update", db_name, "", f"certify_terms {certified}")
        return {"database": db_name, "certified": certified}

    @router.patch(
        "/{db_name}/terms/{term_name}",
        dependencies=get_auth_deps(Permission.UPDATE),
        summary="Edit a business term, or certify its draft expressions",
    )
    async def edit_term(
        db_name: CatalogName,
        term_name: str,
        request: TermEditRequest,
        http: Request,
    ) -> dict[str, Any]:
        """Edit a term's words, or certify expressions as measures.

        Not limited to drafts: expressions mined from query history arrive
        after a catalog is approved, and certifying them is what turns them
        into measures. The engine reads terms at startup.
        """
        if not store.load(db_name):
            raise HTTPException(status_code=404, detail=f"Catalog not found: {db_name}")
        updates = request.model_dump(exclude_none=True)
        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")
        try:
            term = review.update_term(db_name, term_name, updates)
        except KeyError as e:
            raise HTTPException(status_code=400, detail=str(e.args[0])) from e
        # Field and measure names only: never the words, nor an expression's SQL (it may hold literals).
        change = f"edit_term {term_name} {','.join(sorted(updates))}"
        if request.certify_sql:
            change += f" certify_sql={','.join(request.certify_sql)}"
        record(http, "update", db_name, ",".join(term.tables), change)
        return {
            "term": term.name,
            "certified": term.certified,
            "updated_fields": list(updates.keys()),
        }

    return router
