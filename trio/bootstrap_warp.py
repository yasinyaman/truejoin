"""Composition root: builds outbound adapters and wires the `Container`."""

from collections.abc import Callable

from trio.adapters.outbound.audit import LoggingAuditSink
from trio.adapters.outbound.catalog_store.file_store import CatalogFileStore
from trio.adapters.outbound.db.comment_reader import CommentReader
from trio.adapters.outbound.db.dialect import dialect_or_generic
from trio.adapters.outbound.db.factory import DatabaseFactory
from trio.adapters.outbound.db.sample_reader import SampleReader
from trio.adapters.outbound.export.registry import default_exporters
from trio.adapters.outbound.llm.providers import LLMClient
from trio.application.catalog.catalog_analysis import CatalogAnalysisService
from trio.application.catalog.catalog_export import CatalogExportService
from trio.application.catalog.catalog_review import CatalogReviewService
from trio.application.catalog.cross_reference import CrossReferenceService
from trio.application.container import AnalysisFactory, Container
from trio.application.settings import DatabaseConfig, Settings
from trio.ports.audit import AuditSink, NullAuditSink
from trio.ports.catalog_repository import CatalogRepository
from trio.ports.database import DatabaseGateway, DatabaseGatewayFactory
from trio.ports.text_generation import TextGenerator


def build_repository(settings: Settings) -> CatalogFileStore:
    """File store configured from `settings.catalog`."""
    return CatalogFileStore(
        settings.settings.catalog.storage_path,
        default_format=settings.settings.catalog.default_format,
    )


def build_export_service() -> CatalogExportService:
    """Export service over the default exporter registry."""
    return CatalogExportService(default_exporters())


def build_analysis_service(
    *,
    settings: Settings,
    gateway: DatabaseGateway,
    db_config: DatabaseConfig,
    text_generator: TextGenerator,
    repository: CatalogRepository,
) -> CatalogAnalysisService:
    """Wire readers, review and cross-reference around a connected gateway."""
    dialect = dialect_or_generic(db_config.type)
    schema = dialect.default_schema(db_config.database, db_config.options)
    cross_reference = (
        CrossReferenceService(repository, exclude_db=db_config.name)
        if settings.settings.catalog.auto_cross_reference
        else None
    )
    return CatalogAnalysisService(
        gateway=gateway,
        config=settings,
        text_generator=text_generator,
        comments=CommentReader(
            gateway, db_type=dialect, schema=schema, database=db_config.database
        ),
        samples=SampleReader(gateway, db_type=dialect, schema=schema),
        review=CatalogReviewService(repository),
        cross_reference=cross_reference,
        db_type=db_config.type,
        database_name=db_config.name,
    )


def build_container(
    settings: Settings,
    *,
    repository: CatalogRepository | None = None,
    gateway_factory: DatabaseGatewayFactory | None = None,
    text_generator_factory: Callable[[], TextGenerator] | None = None,
    analysis_factory: AnalysisFactory | None = None,
) -> Container:
    """Wire the default adapters; any of them can be replaced (tests, embedding)."""
    repo = repository if repository is not None else build_repository(settings)
    text_generators = text_generator_factory or (lambda: LLMClient.from_config(settings))

    def default_analysis(
        gateway: DatabaseGateway, db_config: DatabaseConfig, text_generator: TextGenerator
    ) -> CatalogAnalysisService:
        return build_analysis_service(
            settings=settings,
            gateway=gateway,
            db_config=db_config,
            text_generator=text_generator,
            repository=repo,
        )

    return Container(
        settings=settings,
        repository=repo,
        review=CatalogReviewService(repo),
        export=build_export_service(),
        gateway_factory=gateway_factory or DatabaseFactory(),
        text_generator_factory=text_generators,
        analysis_factory=analysis_factory or default_analysis,
        audit=_audit_sink(settings),
    )


def _audit_sink(settings: Settings) -> AuditSink:
    """The audit sink for this configuration (a no-op when auditing is off)."""
    config = settings.settings.audit
    if not config.enabled:
        return NullAuditSink()
    return LoggingAuditSink(config.file)
