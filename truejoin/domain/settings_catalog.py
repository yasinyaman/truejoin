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
    "settings.i18n.fallback_language",
    "settings.i18n.auto_translate",
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


def split_label(path: str) -> tuple[str, str]:
    """``(group, label)`` below the section: ``("Reasoning", "Model")``, ``("", "Max limit")``."""
    *groups, leaf = path.removeprefix(_section(path)[0]).split(".")
    readable = [part.replace("_", " ") for part in groups]
    return " · ".join(readable).capitalize(), leaf.replace("_", " ").capitalize()


#: What each setting does, in a line, with its unit. Model slots are below.
HELP: dict[str, str] = {
    "settings.auto_discover_tables": "Find every table of a database and serve it. Off: databases are connected but no table is served.",
    "settings.excluded_tables": "Tables that are never served or analysed, by name.",
    "settings.pagination.default_limit": "Rows a list request returns when it names no limit.",
    "settings.pagination.max_limit": "The largest limit a list request may ask for; a bigger one is refused.",
    "settings.readonly_columns": "Columns clients may never write. Keys and generated columns are always protected.",
    "settings.enable_raw_query": "Serve the raw SQL route of each database. Refused in production.",
    "settings.raw_query_whitelist": "The statements raw SQL may start with.",
    "settings.export.enabled": "Serve the streaming export of each table (read-only).",
    "settings.export.max_rows": "Most rows one export returns. 0 = no limit.",
    "settings.export.batch_size": "Rows read from the database per batch while exporting.",
    "settings.export.statement_timeout_ms": "Milliseconds an export's statement may run. 0 = the driver's default.",
    "settings.masking.enabled": "Mask columns by the semantic type the approved catalog gives them.",
    "settings.masking.rules": "Semantic type -> redact, partial, last4, hash or null.",
    "settings.masking.by_role": "Role -> its own rules, which replace the rules above for that role.",
    "settings.masking.exempt_roles": "Roles that see raw values.",
    "settings.audit.log_filter_columns": "Record the names of the columns a caller filtered on (never the values).",
    "settings.audit.question_text": "Keep the words of a question asked in words in its usage record. A question can name a person.",
    "settings.catalog.default_format": "The format catalogs are stored in: json or yaml.",
    "settings.catalog.auto_cross_reference": "Give analysis the catalogs that already exist as context.",
    "settings.catalog.auto_enrich_openapi": "Add the catalog's descriptions to the OpenAPI spec.",
    "settings.catalog.openapi_enrichment_lang": "The language of the descriptions added to the OpenAPI spec.",
    "settings.catalog.openapi_include_examples": "Put real sample values from the database into the OpenAPI spec. The spec is often readable without a key.",
    "settings.analysis.sample_limit": "Sample rows read per table for the model's context.",
    "settings.analysis.include_row_count": "Include approximate row counts in the analysis.",
    "settings.analysis.excluded_tables": "Tables analysis skips.",
    "settings.analysis.share_samples_with_cloud_llm": "Send sample rows to a cloud model (openai, anthropic, gemini). Off: samples stay on this server.",
    "settings.analysis.mask_pii_samples": "Mask values of columns that look personal before any model sees samples.",
    "settings.analysis.pii_column_patterns": "Column-name patterns treated as personal data.",
    "settings.i18n.default_language": "The language catalog descriptions are written in.",
    "settings.i18n.languages": (
        "The languages an analysis writes descriptions and the words of terms in. Questions "
        "are read with those words: analyze a catalog again after adding a language."
    ),
    "settings.i18n.translation_strategy": (
        "With several languages: ask for all of them in one call (single), or write the "
        "default language and translate the rest after (multi)."
    ),
    "olap.max_ingest_rows": "Most rows pulled from a source when loading a table. 0 = no limit.",
    "olap.full_load_max_rows": "The largest table loaded whole when a query has no usable filter; a bigger one is read by slice or refused.",
    "olap.slice_max_rows": "The largest slice (a filtered read of a table) pulled in one go.",
    "olap.slice_budget_rows": "Rows kept across all slices before the least used are dropped.",
    "olap.semi_join_max_keys": "Most join keys sent to a source to fetch a big table by a small one's keys.",
    "olap.in_chunk_size": "Keys per IN (...) request when fetching by keys.",
    "olap.refresh": 'Tables refreshed incrementally: "db.table" -> {watermark_column, key_columns}.',
    "olap.semantic_models": 'Measures and dimensions per table: "db.table" -> {measures, dimensions}.',
    "olap.cache_ttl": "Seconds a query result stays cached.",
    "olap.cache_max_entries": "Results kept in the cache before the oldest are dropped.",
    "olap.rate_limit": "Requests allowed per API key (else per client), as in 100/minute.",
    "olap.debug_endpoints": "Serve the engine's debug routes. Empty = on outside production, off in production.",
    "olap.backup_enabled": "Back the analytics store up on a schedule.",
    "olap.backup_interval": "Seconds between backups.",
    "olap.backup_path": "The directory backups are written to.",
    "olap.backup_retention_days": "Days a backup is kept.",
    "olap.grounding": "Check joins against declared keys and the approved catalog: advise reports a wrong join, repair fixes it or refuses.",
    "olap.terms": "The term layer: analysis writes business terms, and certified ones answer questions in words.",
    "olap.unread_questions": "A question none of whose words are known: refuse it, or let a model write SQL for it (sql).",
    "olap.remote.url": "Another Truejoin node to read over HTTP. Empty = none.",
    "olap.remote.databases": "Its databases to connect. Empty = every one it lists.",
    "olap.remote.api_key": "The key this server presents to it.",
    "olap.remote.api_key_header": "The header that key is sent in.",
    "olap.remote.timeout": "Seconds to wait for its answer.",
    "olap.remote.max_retries": "Times a failed request to it is tried again.",
    "olap.remote.backoff_factor": "How much longer each retry waits than the one before.",
    "olap.remote.pool_size": "Connections kept open to it.",
    "olap.remote.pool_max_overflow": "Extra connections allowed under load.",
    "olap.remote.circuit_breaker_threshold": "Failures in a row before requests to it are stopped for a while.",
    "olap.remote.circuit_breaker_timeout": "Seconds requests stay stopped before it is tried again.",
    "mcp.profile": "The tools agents get: analyst = read-only tools, admin = all of them.",
    "mcp.tools": "Narrows the profile to these tools, by name. Empty = the whole profile.",
}

#: The same for every model slot (``settings.llm.<slot>.<field>``).
_SLOT_HELP = {
    "provider": "openai, anthropic, gemini or ollama. All but ollama send prompts to a third party.",
    "model": "The model's name at that provider.",
    "api_key": "The provider's key.",
    "base_url": "Where the provider is called. Empty = its usual address.",
    "temperature": "0 to 1: higher varies the wording more.",
    "max_tokens": "The longest answer the model may write, in tokens.",
}

#: What a group of settings is for, shown under its heading.
GROUP_HELP = {
    "settings.llm.default.": "Used for every job a slot below leaves empty.",
    "settings.llm.reasoning.": "Work that needs judgement. Empty = the default model.",
    "settings.llm.non_reasoning.": "Work that needs none, and SQL for unread questions. Empty = the default model.",
    "settings.llm.embed.": "Empty = the default model.",
    "settings.llm.rerank.": "Empty = the default model.",
}

#: A filled-in value for the settings written as JSON.
EXAMPLES = {
    "settings.masking.rules": '{"email": "redact", "name": "partial"}',
    "settings.masking.by_role": '{"support": {"email": "partial"}}',
    "olap.refresh": '{"db.orders": {"watermark_column": "updated_at", "key_columns": ["id"]}}',
    "olap.semantic_models": '{"db.orders": {"measures": [], "dimensions": []}}',
}


def help_of(path: str) -> str:
    if path.startswith("settings.llm."):
        return _SLOT_HELP.get(path.rsplit(".", 1)[-1], "")
    return HELP.get(path, "")


def group_help_of(path: str) -> str:
    return next((text for prefix, text in GROUP_HELP.items() if path.startswith(prefix)), "")
