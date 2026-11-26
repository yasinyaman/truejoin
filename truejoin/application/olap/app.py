"""FusionApp: the assembled application (services + the ports they use)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from truejoin.application.olap.backup import BackupService
from truejoin.application.olap.query import QueryService
from truejoin.application.olap.semantic import SemanticService
from truejoin.application.olap.settings import Settings
from truejoin.application.olap.sources import SourceService
from truejoin.application.olap.tools import ToolService
from truejoin.application.olap.views import MaterializedViewService
from truejoin.domain.olap.catalog import SchemaCatalog
from truejoin.ports.analytics_store import AnalyticsStore
from truejoin.ports.cache import QueryCache
from truejoin.ports.scheduler import Scheduler

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class FusionApp:
    settings: Settings
    catalog: SchemaCatalog
    store: AnalyticsStore
    cache: QueryCache
    scheduler: Scheduler
    sources: SourceService
    query: QueryService
    views: MaterializedViewService
    backup: BackupService
    semantic: SemanticService
    tools: ToolService
    #: Run last by ``close`` (the server's gateway loop thread ends here).
    on_close: list[Callable[[], None]] = field(default_factory=list)
    #: Connects one more gateway database as a source, the way startup did
    #: (its ``DatabaseConfig``). None = this build cannot (no gateway loop).
    attach_database: Callable[[Any], None] | None = None

    def schema_context(self, schemas: list[str] | None = None) -> str:
        """LLM-friendly Markdown description of the connected schemas."""
        return self.catalog.generate_context(schemas)

    def start_refresh(self) -> None:
        """Reload what is loaded every ``refresh_interval`` seconds, when it is set.

        Whole tables are fetched again (topped up by their watermark when
        ``olap.refresh`` names one) and published by a rename, so a query sees
        the old rows or the new ones; slices are dropped, to be fetched again
        by the next query that needs them. The cache is cleared after.
        """
        if self.settings.refresh_interval > 0:
            self.sources.start_auto_refresh(self.settings.refresh_interval)

    def close(self) -> None:
        """Stop timers, close sources and the store (idempotent)."""
        self.sources.stop_auto_refresh()
        self.views.close()
        self.backup.stop()
        self.sources.close_all()
        self.scheduler.cancel_all()
        try:
            self.store.close()
        except Exception as e:
            logger.warning("Error closing store: %s", e)
        for callback in self.on_close:
            callback()
        logger.info("FusionApp closed")

    def __enter__(self) -> FusionApp:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
