"""Default exporter registry."""

from trio.adapters.outbound.export.json import JsonExporter
from trio.adapters.outbound.export.markdown import MarkdownExporter
from trio.adapters.outbound.export.yaml import YamlExporter
from trio.ports.exporter import CatalogExporter


def default_exporters() -> dict[str, CatalogExporter]:
    """Exporters keyed by format name."""
    markdown = MarkdownExporter()
    return {"json": JsonExporter(), "yaml": YamlExporter(), "markdown": markdown, "md": markdown}
