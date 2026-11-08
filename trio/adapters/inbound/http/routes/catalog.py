"""Catalog REST API endpoints.

Provides REST endpoints for catalog operations:
- Analyze database and generate catalog
- List available catalogs
- Get catalog info
- Export catalog
- Enrich OpenAPI spec
"""

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import uuid4

import yaml
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi import Path as PathParam
from fastapi.responses import Response
from pydantic import BaseModel, Field
from pydantic import ValidationError as PydanticValidationError

from trio.adapters.inbound.http.auth import AuthManager, Permission, caller_of
from trio.adapters.inbound.http.olap import masked_types
from trio.adapters.inbound.http.request_context import request_id_of
from trio.application.catalog.openapi_enrichment import OpenAPIEnricher, withheld_from_spec
from trio.application.catalog.search import search_catalogs
from trio.application.container import Container
from trio.domain.catalog.catalog import CatalogStatus, DatabaseCatalog, structural_catalog
from trio.domain.catalog.catalog_naming import CATALOG_NAME_PATTERN
from trio.domain.catalog.examples import ExampleError
from trio.domain.catalog.terms import MeasureError, Term
from trio.domain.gateway.errors import (
    AnalysisError,
    ColumnNotFoundInCatalogError,
    DatabaseNotConfiguredError,
    LLMError,
    TableNotFoundInCatalogError,
    UnsupportedExportFormatError,
)
from trio.domain.gateway.samples import looks_personal
from trio.domain.gateway.schema import DatabaseSchema
from trio.domain.governance.audit import AuditAction, AuditEvent
from trio.ports.database import DatabaseGateway
from trio.ports.query_history import ExpressionChecker

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


class AnalysisJob(BaseModel):
    """An analysis running in the background, or how it ended."""

    id: str
    database: str
    tables: list[str] | None = None
    state: Literal["running", "done", "failed"] = "running"
    started_at: datetime
    finished_at: datetime | None = None
    result: AnalyzeResponse | None = Field(default=None, description="Once done")
    error: str | None = Field(default=None, description="Once failed")


#: A file name's extension for the export formats whose name is not one.
_EXTENSIONS = {"markdown": "md", "ossie": "ossie.yaml"}

#: A catalog file larger than this is not a catalog.
MAX_IMPORT_BYTES = 20 * 1024 * 1024

#: Finished jobs kept for a late look; running ones are always kept.
KEPT_JOBS = 50


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
    glossary: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


TagName = Annotated[
    str,
    PathParam(min_length=1, max_length=40, description="A tag, as tables and columns carry it"),
]


class TagTarget(BaseModel):
    """A table, or one of its columns."""

    table: str = Field(..., max_length=128)
    column: str | None = Field(default=None, max_length=128)


class TagRequest(BaseModel):
    """Put a tag on these tables and columns, or take it off them."""

    targets: list[TagTarget] = Field(..., min_length=1, max_length=1000)
    remove: bool = False


class TagRenameRequest(BaseModel):
    """A tag's new name; an existing one merges."""

    name: str = Field(..., min_length=1, max_length=40)


class ExampleRequest(BaseModel):
    """A question and the SQL that answers it, saved as a draft."""

    name: str = Field(..., min_length=1, max_length=63)
    question: str = Field(..., min_length=1, max_length=1000)
    sql: str = Field(..., min_length=1, max_length=20000)
    description: str = Field(default="", max_length=2000)
    lang: str | None = None


class ExampleEditRequest(BaseModel):
    """Certify an example, or take its certification back."""

    certified: bool


OwnerName = Annotated[str, Field(max_length=200)]


class OwnersRequest(BaseModel):
    """Who answers for tables and terms; an empty name removes an owner."""

    tables: dict[str, OwnerName] | None = Field(default=None, max_length=1000)
    terms: dict[str, OwnerName] | None = Field(default=None, max_length=1000)


class GuidanceRequest(BaseModel):
    """The catalog's glossary and notes for a model; each, when given, replaces the old."""

    glossary: (
        dict[
            Annotated[str, Field(min_length=1, max_length=60)],
            Annotated[str, Field(min_length=1, max_length=200)],
        ]
        | None
    ) = Field(
        default=None,
        max_length=500,
        description="Abbreviation -> the words it stands for, as questions use them",
    )
    notes: list[Annotated[str, Field(min_length=1, max_length=1000)]] | None = Field(
        default=None, max_length=50, description="What a model should know about this data"
    )


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
    owner: str | None = Field(default=None, description="Who answers for the table")


class MeasureCheckRequest(BaseModel):
    """An expression to try as a measure on one table."""

    table: str = Field(..., max_length=128, description="The table it reads, one the term covers")
    expression: str = Field(
        ...,
        max_length=2000,
        description="DuckDB SQL over the table's columns: what goes inside the aggregate",
    )


class MeasureRequest(MeasureCheckRequest):
    """A measure a reviewer writes; stored as a draft until certified."""

    name: str = Field(..., max_length=63, description="An identifier, free on the table")
    description: str = Field(default="", max_length=1000)
    lang: str | None = Field(default=None, description="The description's language")


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
    on_change: Callable[[str], object] | None = None,
) -> APIRouter:
    """Create the catalog API router.

    Permissions (enforced when `auth_manager` is enabled):
        read   - list/info/export/draft views
        create - POST /analyze and /analyze/jobs (writes a new catalog, spends LLM budget)
        update - draft edits, approvals and re-opening
        delete - DELETE /{db_name}

    Args:
        container: The wired application services (repository, review, export,
            analysis).
        gateways: Connected database gateways by name (for live analysis).
        app: FastAPI app instance (for OpenAPI enrichment refresh)
        auth_manager: Optional auth manager; when enabled every endpoint
            requires an API key with the matching permission.
        on_change: Called with the database name after every change, so an
            approved catalog goes live without a restart.

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
        # The rule find_examples follows: a caller with row rules or masks sees
        # only certified examples that quote no value (another tenant's, maybe).
        caller = caller_of(http)
        if caller is not None and (
            caller.has_row_rules or masked_types(container.settings.settings.masking, caller.roles)
        ):
            catalog.examples = {
                name: example
                for name, example in catalog.examples.items()
                if example.certified and not example.has_literals
            }
        return catalog

    def record(http: Request, action: AuditAction, db_name: str, table: str, change: str) -> None:
        """One audit event per catalog change that took effect (the sink never raises)."""
        if on_change is not None:
            on_change(db_name)
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

    # Before "/{db_name}", which would otherwise take "search" for a catalog's name.
    @router.get(
        "/search",
        dependencies=get_auth_deps(Permission.READ),
        summary="Search tables, columns, terms, measures and abbreviations",
    )
    async def search(
        http: Request,
        q: str = Query(..., min_length=1, max_length=200, description="Words, all to be found"),
        database: str | None = Query(default=None, description="Only this catalog"),
        lang: str | None = Query(default=None, description="Labels in this language"),
        limit: int = Query(default=50, ge=1, le=200),
    ) -> dict[str, Any]:
        """Every word of ``q`` in a name, a synonym, a tag or a description, in any status.

        Names rank first, then synonyms and tags, then descriptions. Stored
        values are not searched.
        """
        names = [database] if database else sorted(store.get_index().catalogs)
        catalogs = []
        for name in names:
            try:
                catalog = store.load(name)
            except Exception as e:  # one unreadable catalog must not end the search
                logger.warning(f"Could not read catalog {name} for search: {e}")
                continue
            if catalog is not None:
                catalogs.append(visible_to(http, catalog))
        language = lang or container.settings.settings.i18n.default_language
        hits = search_catalogs(catalogs, q, language, limit)
        return {"query": q, "hits": hits, "count": len(hits)}

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
        lang: str = Query(
            default="en",
            description="Language; 'all' writes the whole catalog, which is what import reads",
        ),
    ) -> Response:
        """Export a catalog in the specified format."""
        catalog = store.load(db_name)
        if not catalog:
            raise HTTPException(status_code=404, detail=f"Catalog not found: {db_name}")
        try:
            content = container.export.render(
                visible_to(http, catalog), format, lang=None if lang == "all" else lang
            )
        except UnsupportedExportFormatError as e:
            raise HTTPException(status_code=400, detail=e.message) from e

        return Response(
            content=content,
            media_type=container.export.media_type(format),
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{db_name}_catalog.{_EXTENSIONS.get(format, format)}"'
                )
            },
        )

    @router.post(
        "/{db_name}/import",
        status_code=201,
        dependencies=get_auth_deps(Permission.CREATE),
        summary="Import a catalog exported with lang=all, as a draft",
    )
    async def import_catalog(
        db_name: CatalogName,
        http: Request,
        replace: bool = Query(default=False, description="Replace the catalog there is"),
    ) -> dict[str, Any]:
        """A catalog from another install (JSON or YAML, as ``export?lang=all`` writes it).

        Stored under ``db_name`` as a draft with every table pending and every
        term and measure uncertified: nobody here has reviewed it, so nothing
        of it goes live until it is approved here.
        """
        body = await http.body()
        if len(body) > MAX_IMPORT_BYTES:
            raise HTTPException(status_code=413, detail="A catalog to import is at most 20 MB")
        try:
            text = body.decode("utf-8-sig")
            try:
                document = json.loads(text)
            except json.JSONDecodeError:
                document = yaml.safe_load(text)
        except (UnicodeDecodeError, yaml.YAMLError) as e:
            raise HTTPException(status_code=422, detail=f"Not JSON or YAML: {e}") from e
        if not isinstance(document, dict):
            raise HTTPException(status_code=422, detail="A catalog is a JSON or YAML object")
        if "_language" in document:
            raise HTTPException(
                status_code=422,
                detail="This export holds one language only; export with lang=all to import it",
            )
        if store.load(db_name) and not replace:
            raise HTTPException(
                status_code=409,
                detail=f"{db_name} has a catalog; import with replace=true to replace it",
            )
        try:
            catalog = DatabaseCatalog.model_validate({**document, "database_name": db_name})
        except PydanticValidationError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        review.import_catalog(catalog)
        record(http, "create", db_name, "", "import" + (" replace" if replace else ""))
        if app and adapters:
            _refresh_openapi_enrichment(app, container, adapters)
        return {
            "database": db_name,
            "status": catalog.status.value,
            "tables": catalog.table_count,
            "terms": len(catalog.terms),
        }

    def connected(database: str) -> None:
        if not adapters:
            raise HTTPException(
                status_code=503,
                detail="Analysis not available - no database gateways are connected",
            )
        if database not in adapters:
            raise HTTPException(status_code=404, detail=f"Database not connected: {database}")

    async def run_analysis(request: AnalyzeRequest, http: Request) -> AnalyzeResponse:
        connected(request.database)
        adapter = adapters[request.database]  # type: ignore[index]
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
        A large database can outlast a proxy's timeout: use
        ``POST /catalog/analyze/jobs`` for that.
        """
        return await run_analysis(request, http)

    # shortcut: jobs live in the process that started them; behind several API
    # workers a poll can land on another one and get 404. Move them to the
    # catalog store if the gateway is ever run on more than one worker.
    jobs: dict[str, AnalysisJob] = {}
    running: set[asyncio.Task[None]] = set()  # the loop holds tasks weakly

    async def work(job: AnalysisJob, request: AnalyzeRequest, http: Request) -> None:
        try:
            job.result = await run_analysis(request, http)
            job.state = "done"
        except HTTPException as e:
            job.error, job.state = str(e.detail), "failed"
        finally:
            job.finished_at = datetime.now(UTC)

    @router.post(
        "/analyze/jobs",
        status_code=202,
        dependencies=get_auth_deps(Permission.CREATE),
        response_model=AnalysisJob,
        summary="Start an analysis in the background",
    )
    async def start_analysis(request: AnalyzeRequest, http: Request) -> AnalysisJob:
        """What ``POST /catalog/analyze`` does, without holding the request open.

        Poll ``GET /catalog/analyze/jobs/{job_id}`` for the outcome. One
        analysis per database at a time: a second answers 409.
        """
        connected(request.database)
        for job in jobs.values():
            if job.database == request.database and job.state == "running":
                raise HTTPException(
                    status_code=409,
                    detail=f"An analysis of {request.database} is already running (job {job.id})",
                )
        finished = [j.id for j in jobs.values() if j.state != "running"]
        for old in finished[: max(0, len(finished) - KEPT_JOBS + 1)]:
            del jobs[old]
        job = AnalysisJob(
            id=uuid4().hex,
            database=request.database,
            tables=request.tables,
            started_at=datetime.now(UTC),
        )
        jobs[job.id] = job
        task = asyncio.create_task(work(job, request, http))
        running.add(task)
        task.add_done_callback(running.discard)
        return job

    @router.get(
        "/analyze/jobs",
        dependencies=get_auth_deps(Permission.READ),
        response_model=list[AnalysisJob],
        summary="Background analyses, newest first",
    )
    async def list_analyses() -> list[AnalysisJob]:
        return list(reversed(jobs.values()))

    @router.get(
        "/analyze/jobs/{job_id}",
        dependencies=get_auth_deps(Permission.READ),
        response_model=AnalysisJob,
        summary="One background analysis",
    )
    async def get_analysis(job_id: str) -> AnalysisJob:
        job = jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"No such analysis job: {job_id}")
        return job

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
            glossary=catalog.glossary,
            notes=catalog.notes,
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
                {
                    **_term_detail(term, table_name, withheld),
                    "owner": catalog.term_owners.get(term.name),
                }
                for term in catalog.terms.values()
                if table_name in term.tables
            ],
            owner=catalog.table_owners.get(table_name),
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
        "/{db_name}/reopen",
        dependencies=get_auth_deps(Permission.UPDATE),
        summary="Re-open an approved catalog for review",
    )
    async def reopen_catalog(db_name: CatalogName, http: Request) -> dict[str, Any]:
        """Turn an approved catalog back into a draft, every table pending, to edit it.

        What `trio catalog review` asks before reviewing an approved catalog.
        What is live stays live (masks, joins, terms) until it is approved
        again; certified terms stay certified.
        """
        catalog = store.load(db_name)
        if not catalog:
            raise HTTPException(status_code=404, detail=f"Catalog not found: {db_name}")
        if catalog.status != CatalogStatus.approved:
            raise HTTPException(status_code=409, detail=f"Catalog is already a draft: {db_name}")
        review.save_as_draft(catalog)
        record(http, "update", db_name, "", "reopen")
        return {"database": db_name, "status": CatalogStatus.draft.value}

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
        into measures, live at once.
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

    @router.patch(
        "/{db_name}/guidance",
        dependencies=get_auth_deps(Permission.UPDATE),
        summary="Set the glossary and the notes a model reads",
    )
    async def edit_guidance(
        db_name: CatalogName, request: GuidanceRequest, http: Request
    ) -> dict[str, Any]:
        """Abbreviations questions use, and what a model should know about the data.

        The glossary is written out before a question is matched
        (``answer_question``, ``search_terms``); the notes come with the
        source in ``list_sources``. Not limited to drafts; on an approved
        catalog they are used at once.
        """
        if not store.load(db_name):
            raise HTTPException(status_code=404, detail=f"Catalog not found: {db_name}")
        updates = request.model_dump(exclude_none=True)
        if not updates:
            raise HTTPException(status_code=400, detail="No fields to update")
        catalog = review.set_guidance(db_name, **updates)
        # The OpenAPI context carries them too; rebuilt now rather than at the next poll.
        if app and adapters:
            _refresh_openapi_enrichment(app, container, adapters)
        # Which fields only: the words themselves are a reviewer's text.
        record(http, "update", db_name, "", f"edit_guidance {','.join(sorted(updates))}")
        return {"database": db_name, "glossary": catalog.glossary, "notes": catalog.notes}

    @router.patch(
        "/{db_name}/owners",
        dependencies=get_auth_deps(Permission.UPDATE),
        summary="Say who answers for tables and terms",
    )
    async def edit_owners(
        db_name: CatalogName, request: OwnersRequest, http: Request
    ) -> dict[str, Any]:
        """Not part of the review: a catalog of any status takes it, at once."""
        if not store.load(db_name):
            raise HTTPException(status_code=404, detail=f"Catalog not found: {db_name}")
        if request.tables is None and request.terms is None:
            raise HTTPException(status_code=400, detail="No owners given")
        try:
            catalog = review.set_owners(db_name, request.tables, request.terms)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e.args[0])) from e
        named = sorted(request.tables or {}) + [f"term:{n}" for n in sorted(request.terms or {})]
        record(http, "update", db_name, "", f"owners {','.join(named)}")
        return {
            "database": db_name,
            "tables": catalog.table_owners,
            "terms": catalog.term_owners,
        }

    @router.post(
        "/{db_name}/examples",
        status_code=201,
        dependencies=get_auth_deps(Permission.UPDATE),
        summary="Save an example: a question and the SQL that answers it, as a draft",
    )
    async def add_example(
        db_name: CatalogName, request: ExampleRequest, http: Request
    ) -> dict[str, Any]:
        """Its SQL must pass the engine's guardrails and read this catalog's tables only.

        Nothing offers it to a model (``find_examples``) until it is certified.
        """
        catalog = store.load(db_name)
        if not catalog:
            raise HTTPException(status_code=404, detail=f"Catalog not found: {db_name}")
        if container.sql_validator is None or container.sql_analyzer is None:
            raise HTTPException(status_code=501, detail="This build cannot check examples")
        try:
            example = review.add_example(
                db_name,
                name=request.name,
                question=request.question,
                sql=request.sql,
                description=request.description,
                lang=request.lang or (catalog.languages[0] if catalog.languages else "en"),
                validator=container.sql_validator,
                analyzer=container.sql_analyzer,
            )
        except ExampleError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        record(http, "update", db_name, ",".join(example.tables), f"add_example {example.name}")
        return example.model_dump(mode="json")

    @router.patch(
        "/{db_name}/examples/{name}",
        dependencies=get_auth_deps(Permission.UPDATE),
        summary="Certify an example, or take it back",
    )
    async def edit_example(
        db_name: CatalogName, name: str, request: ExampleEditRequest, http: Request
    ) -> dict[str, Any]:
        if not store.load(db_name):
            raise HTTPException(status_code=404, detail=f"Catalog not found: {db_name}")
        try:
            example = review.certify_example(db_name, name, request.certified)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e.args[0])) from e
        verb = "certify_example" if request.certified else "uncertify_example"
        record(http, "update", db_name, ",".join(example.tables), f"{verb} {name}")
        return example.model_dump(mode="json")

    @router.delete(
        "/{db_name}/examples/{name}",
        dependencies=get_auth_deps(Permission.UPDATE),
        summary="Remove an example",
    )
    async def remove_example(db_name: CatalogName, name: str, http: Request) -> dict[str, Any]:
        if not store.load(db_name):
            raise HTTPException(status_code=404, detail=f"Catalog not found: {db_name}")
        try:
            removed = review.remove_example(db_name, name)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e.args[0])) from e
        record(http, "update", db_name, ",".join(removed.tables), f"remove_example {name}")
        return {"database": db_name, "removed": name, "was_certified": removed.certified}

    def draft_or_409(db_name: str) -> DatabaseCatalog:
        catalog = store.load(db_name)
        if not catalog:
            raise HTTPException(status_code=404, detail=f"Catalog not found: {db_name}")
        if catalog.status != CatalogStatus.draft:
            raise HTTPException(
                status_code=409,
                detail=f"Catalog is not in draft status: {db_name}. Re-open it for review "
                "to change its tags: a tag can make a column personal.",
            )
        return catalog

    @router.get(
        "/{db_name}/tags",
        dependencies=get_auth_deps(Permission.READ),
        summary="Every tag of a catalog and the tables and columns that carry it",
    )
    async def list_tags(db_name: CatalogName) -> dict[str, Any]:
        if not store.load(db_name):
            raise HTTPException(status_code=404, detail=f"Catalog not found: {db_name}")
        return {"database": db_name, "tags": review.tags(db_name)}

    @router.post(
        "/{db_name}/tags/{tag}",
        dependencies=get_auth_deps(Permission.UPDATE),
        summary="Put a tag on many tables and columns at once, or take it off",
    )
    async def apply_tag(
        db_name: CatalogName, tag: TagName, request: TagRequest, http: Request
    ) -> dict[str, Any]:
        """Each change is the edit a reviewer would make one by one: kept as an
        override, its table marked edited. Drafts only, as other edits are.
        """
        draft_or_409(db_name)
        name = tag.strip()
        if not name:
            raise HTTPException(status_code=422, detail="A tag needs a name")
        try:
            changed = review.tag_targets(
                db_name, name, [(t.table, t.column) for t in request.targets], request.remove
            )
        except (TableNotFoundInCatalogError, ColumnNotFoundInCatalogError) as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        if changed:
            verb = "untag" if request.remove else "tag"
            record(http, "update", db_name, "", f"{verb} {name} x{changed}")
        return {"tag": name, "changed": changed}

    @router.patch(
        "/{db_name}/tags/{tag}",
        dependencies=get_auth_deps(Permission.UPDATE),
        summary="Rename a tag everywhere",
    )
    async def rename_tag(
        db_name: CatalogName, tag: TagName, request: TagRenameRequest, http: Request
    ) -> dict[str, Any]:
        draft_or_409(db_name)
        new = request.name.strip()
        if not new:
            raise HTTPException(status_code=422, detail="A tag needs a name")
        changed = review.rename_tag(db_name, tag, new)
        if not changed:
            raise HTTPException(status_code=404, detail=f"No tag '{tag}' in {db_name}")
        record(http, "update", db_name, "", f"rename_tag {tag} -> {new} x{changed}")
        return {"tag": new, "changed": changed}

    @router.delete(
        "/{db_name}/tags/{tag}",
        dependencies=get_auth_deps(Permission.UPDATE),
        summary="Take a tag off everything",
    )
    async def remove_tag(db_name: CatalogName, tag: TagName, http: Request) -> dict[str, Any]:
        draft_or_409(db_name)
        changed = review.remove_tag(db_name, tag)
        if not changed:
            raise HTTPException(status_code=404, detail=f"No tag '{tag}' in {db_name}")
        record(http, "update", db_name, "", f"remove_tag {tag} x{changed}")
        return {"tag": tag, "changed": changed}

    def measured_term(db_name: str, term_name: str, table: str) -> DatabaseCatalog:
        """The catalog, once ``term_name`` is in it and covers ``table``; else 404 or 422."""
        catalog = store.load(db_name)
        if not catalog:
            raise HTTPException(status_code=404, detail=f"Catalog not found: {db_name}")
        term = catalog.terms.get(term_name)
        if term is None:
            raise HTTPException(status_code=404, detail=f"No term '{term_name}' in {db_name}")
        if table not in term.tables:
            raise HTTPException(
                status_code=422,
                detail=f"Term '{term_name}' covers {', '.join(term.tables) or 'no table'}, "
                f"not '{table}'",
            )
        return catalog

    def checker() -> ExpressionChecker:
        if container.expressions is None:
            raise HTTPException(status_code=501, detail="This build cannot check measures")
        return container.expressions

    @router.post(
        "/{db_name}/terms/{term_name}/measures/check",
        dependencies=get_auth_deps(Permission.UPDATE),
        summary="Check an expression as a measure, without storing it",
    )
    async def check_measure(
        db_name: CatalogName, term_name: str, request: MeasureCheckRequest
    ) -> dict[str, Any]:
        """What ``POST .../measures`` checks: the shape, and a trial sum in DuckDB.

        Answers the expression as it would be stored and the columns it
        reads, or 422 saying what is wrong with it.
        """
        measured_term(db_name, term_name, request.table)
        try:
            expression, reads = review.check_measure(
                db_name, request.table, request.expression, checker()
            )
        except MeasureError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        return {"expression": expression, "reads": list(reads)}

    @router.post(
        "/{db_name}/terms/{term_name}/measures",
        status_code=201,
        dependencies=get_auth_deps(Permission.UPDATE),
        summary="Write a measure by hand, as a draft",
    )
    async def add_measure(
        db_name: CatalogName, term_name: str, request: MeasureRequest, http: Request
    ) -> dict[str, Any]:
        """Check an expression and store it on the term, uncertified.

        It is no measure until it is certified (``certify_sql`` on the term),
        exactly like one suggested from the query history.
        """
        catalog = measured_term(db_name, term_name, request.table)
        lang = request.lang or (catalog.languages[0] if catalog.languages else "en")
        try:
            measure = review.add_measure(
                db_name,
                term_name,
                table=request.table,
                name=request.name,
                expression=request.expression,
                description=request.description,
                lang=lang,
                checker=checker(),
            )
        except MeasureError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        # The name only: an expression may hold literals.
        record(http, "update", db_name, request.table, f"add_measure {measure.name}")
        return {
            "term": term_name,
            "name": measure.name,
            "table": measure.table,
            "expression": measure.expression,
            "certified": measure.certified,
        }

    @router.delete(
        "/{db_name}/terms/{term_name}/measures/{name}",
        dependencies=get_auth_deps(Permission.UPDATE),
        summary="Remove a draft or certified measure from its term",
    )
    async def remove_measure(
        db_name: CatalogName, term_name: str, name: str, http: Request
    ) -> dict[str, Any]:
        """A certified one stops being offered at once."""
        if not store.load(db_name):
            raise HTTPException(status_code=404, detail=f"Catalog not found: {db_name}")
        try:
            removed = review.remove_measure(db_name, term_name, name)
        except KeyError as e:
            raise HTTPException(status_code=404, detail=str(e.args[0])) from e
        record(http, "update", db_name, removed.table, f"remove_measure {removed.name}")
        return {"term": term_name, "removed": removed.name, "was_certified": removed.certified}

    return router
