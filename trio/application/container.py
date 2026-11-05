"""The application's wired object graph (built by `infrastructure.bootstrap`).

Inbound adapters (HTTP, CLI) receive a `Container` and never construct
outbound adapters themselves; everything they need is a port or a service.
"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from trio.application.catalog.catalog_analysis import CatalogAnalysisService
from trio.application.catalog.catalog_export import CatalogExportService
from trio.application.catalog.catalog_review import CatalogReviewService
from trio.application.catalog.pipeline import PipelineService
from trio.application.olap.app import FusionApp
from trio.application.settings import DatabaseConfig, Settings
from trio.ports.audit import AuditSink, NullAuditSink
from trio.ports.catalog_repository import CatalogRepository
from trio.ports.database import DatabaseGateway, DatabaseGatewayFactory
from trio.ports.text_generation import TextGenerator

AnalysisFactory = Callable[[DatabaseGateway, DatabaseConfig, TextGenerator], CatalogAnalysisService]


@dataclass(frozen=True)
class Container:
    """Services and port implementations for one configuration."""

    settings: Settings
    repository: CatalogRepository
    review: CatalogReviewService
    export: CatalogExportService
    gateway_factory: DatabaseGatewayFactory
    text_generator_factory: Callable[[], TextGenerator]
    analysis_factory: AnalysisFactory
    #: The model for work that needs no judgement (``llm.non_reasoning``).
    #: None when that slot is not configured: the reasoning model does it.
    light_generator_factory: Callable[[], TextGenerator] | None = None
    #: Where data-access events go. Built by the composition root, because an
    #: inbound adapter may not reach for an outbound one itself.
    audit: AuditSink = field(default_factory=NullAuditSink)
    #: Builds the OLAP side for an ``APP_ENV`` (loop thread, DuckDB, one source
    #: per database). The caller closes what it returns. None = no OLAP side.
    olap_factory: Callable[[str], FusionApp] | None = None

    def database(self, name: str) -> DatabaseConfig:
        """Configuration of the database called `name` (404-style error if unknown)."""
        return self.settings.database(name)

    @asynccontextmanager
    async def open_gateway(self, db_config: DatabaseConfig) -> AsyncIterator[DatabaseGateway]:
        """Create, connect and finally disconnect a gateway for `db_config`."""
        gateway = self.gateway_factory.create(db_config)
        await gateway.connect()
        try:
            yield gateway
        finally:
            await gateway.disconnect()

    @asynccontextmanager
    async def open_analysis(
        self, db_name: str, *, gateway: DatabaseGateway | None = None
    ) -> AsyncIterator[CatalogAnalysisService]:
        """Yield an analysis service for `db_name`.

        A `gateway` passed in is reused (and not closed); otherwise one is
        opened for the duration. The text generator is always created and
        closed here.

        Raises:
            DatabaseNotConfiguredError: If `db_name` is not configured.
        """
        db_config = self.database(db_name)
        if gateway is None:
            async with (
                self.open_gateway(db_config) as owned,
                self._analysis(owned, db_config) as analysis,
            ):
                yield analysis
        else:
            async with self._analysis(gateway, db_config) as analysis:
                yield analysis

    @asynccontextmanager
    async def _analysis(
        self, gateway: DatabaseGateway, db_config: DatabaseConfig
    ) -> AsyncIterator[CatalogAnalysisService]:
        text_generator = self.text_generator_factory()
        light = self.light_generator_factory() if self.light_generator_factory else None
        try:
            analysis = self.analysis_factory(gateway, db_config, text_generator)
            analysis.light_generator = light
            yield analysis
        finally:
            await text_generator.close()
            if light is not None:
                await light.close()

    def pipeline(self) -> PipelineService:
        """Pipeline service bound to this container."""
        return PipelineService(
            settings=self.settings,
            analysis_opener=self.open_analysis,
            export=self.export,
        )
