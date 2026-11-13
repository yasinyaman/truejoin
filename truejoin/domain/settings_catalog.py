"""Which settings the console may change, and which stay in the configuration file.

A setting is named by its dotted path in the configuration (``settings.
pagination.max_limit``, ``olap.cache_ttl``, ``mcp.tools``). Three kinds:

- **bootstrap**: what the server needs before it can read anything else (the
  stores, the URL layout, the DuckDB core) and what an intruder could use to
  lock everyone out or hide their tracks (authentication, the audit trail on
  or off, the pseudonym key). These are read from the file only.
- **dangerous**: settings the console may change, but only with the setting's
  name typed back, because they can expose data or send it elsewhere.
- **secret**: kept encrypted, never shown; a blank value keeps the one kept.

Everything else the configuration models define may be changed from the
console, unless the file writes it, which locks it there.
"""

from __future__ import annotations

from collections.abc import Iterable

#: Read from the file only. A path ending in ``.`` covers everything under it.
BOOTSTRAP: tuple[str, ...] = (
    "settings.api_prefix",
    "settings.auth.",
    "settings.database_store.",
    "settings.settings_store.",
    "settings.catalog.storage_path",
    "settings.audit.enabled",
    "settings.audit.file",
    "settings.masking.hash_secret",
    "olap.enabled",
    "olap.database",
    "olap.memory_limit",
    "olap.threads",
    "olap.external_access",
    "olap.temp_directory",
    "olap.max_temp_directory_size",
    "mcp.http_path",
    "mcp.stdio_api_key_name",
)

#: Nothing reads them; the console does not offer them.
HIDDEN: tuple[str, ...] = (
    "settings.docs_url",
    "settings.redoc_url",
    "settings.llm.language_prompts",
    "settings.i18n.languages",
    "settings.i18n.fallback_language",
    "settings.i18n.auto_translate",
    "settings.i18n.translation_strategy",
)

#: Changed only with the path typed back: each can expose data or send it elsewhere.
DANGEROUS: tuple[str, ...] = (
    "settings.enable_raw_query",
    "settings.raw_query_whitelist",
    "settings.masking.",
    "settings.analysis.share_samples_with_cloud_llm",
    "settings.analysis.mask_pii_samples",
    "settings.catalog.openapi_include_examples",
    "settings.llm.",  # provider and base_url decide where samples are sent
    "settings.audit.question_text",
    "olap.remote.",
    "olap.backup_path",
    "olap.debug_endpoints",
    "mcp.profile",
    "mcp.tools",
)

#: Kept encrypted and never shown.
SECRET_SUFFIXES: tuple[str, ...] = (".api_key",)

#: The console's sections, in order: (path prefix, title).
SECTIONS: tuple[tuple[str, str], ...] = (
    ("settings.pagination.", "API"),
    ("settings.export.", "Export"),
    ("settings.masking.", "Masking"),
    ("settings.audit.", "Audit"),
    ("settings.catalog.", "Catalog"),
    ("settings.analysis.", "Analysis"),
    ("settings.llm.", "Models"),
    ("settings.i18n.", "Language"),
    ("olap.remote.", "Remote sources"),
    ("olap.", "Analytics engine"),
    ("mcp.", "Agents (MCP)"),
    ("settings.", "API"),
)


def _covers(rules: Iterable[str], path: str) -> bool:
    return any(path == r or (r.endswith(".") and path.startswith(r)) for r in rules)


def is_bootstrap(path: str) -> bool:
    return _covers(BOOTSTRAP, path)


def is_hidden(path: str) -> bool:
    return _covers(HIDDEN, path)


def is_dangerous(path: str) -> bool:
    return _covers(DANGEROUS, path) and not is_secret(path)


def is_secret(path: str) -> bool:
    return path.endswith(SECRET_SUFFIXES)


def _section(path: str) -> tuple[str, str]:
    return next((prefix, title) for prefix, title in SECTIONS if path.startswith(prefix))


#: Where the bootstrap settings are listed, together: read-only, from the file.
STARTUP = "Startup (file only)"


def section_of(path: str) -> str:
    return STARTUP if is_bootstrap(path) else _section(path)[1]


def label_of(path: str) -> str:
    """The path below its section, readable: ``reasoning · model``, ``max limit``."""
    rest = path.removeprefix(_section(path)[0])
    return " · ".join(part.replace("_", " ") for part in rest.split(".")).capitalize()
