"""Catalog export use case over a registry of exporters."""

from collections.abc import Mapping
from pathlib import Path

from trio.domain.catalog.catalog import DatabaseCatalog
from trio.domain.gateway.errors import UnsupportedExportFormatError
from trio.ports.exporter import CatalogExporter

_MEDIA_TYPES = {"json": "application/json", "yaml": "application/x-yaml"}


class CatalogExportService:
    """Renders/exports catalogs in the registered formats."""

    def __init__(self, exporters: Mapping[str, CatalogExporter]):
        """Register `exporters` by format name (e.g. json, yaml, markdown, md)."""
        self._exporters = dict(exporters)

    @property
    def formats(self) -> list[str]:
        """Registered format names."""
        return list(self._exporters)

    def get(self, fmt: str) -> CatalogExporter:
        """Exporter for `fmt` or `UnsupportedExportFormatError` (400)."""
        try:
            return self._exporters[fmt]
        except KeyError:
            raise UnsupportedExportFormatError(fmt, self.formats) from None

    def render(self, catalog: DatabaseCatalog, fmt: str, lang: str | None = None) -> str:
        """Export to a string."""
        return self.get(fmt).export_string(catalog, lang=lang)

    def export(
        self, catalog: DatabaseCatalog, fmt: str, output_path: str | Path, lang: str | None = None
    ) -> Path:
        """Export to a file; returns the written path."""
        return self.get(fmt).export(catalog, output_path, lang=lang)

    @staticmethod
    def media_type(fmt: str) -> str:
        """HTTP media type for `fmt`."""
        return _MEDIA_TYPES.get(fmt, "text/markdown")
