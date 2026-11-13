"""Default exporter registry."""

from truejoin.adapters.outbound.export.json import JsonExporter
from truejoin.adapters.outbound.export.markdown import MarkdownExporter
from truejoin.adapters.outbound.export.ossie import OssieExporter
from truejoin.adapters.outbound.export.yaml import YamlExporter
from truejoin.ports.exporter import CatalogExporter


def default_exporters() -> dict[str, CatalogExporter]:
    """Exporters keyed by format name."""
    markdown = MarkdownExporter()
    return {
        "json": JsonExporter(),
        "yaml": YamlExporter(),
        "markdown": markdown,
        "md": markdown,
        "ossie": OssieExporter(),
    }
