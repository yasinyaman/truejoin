"""Application configuration models and production safety validation."""

import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from trio.application.olap.settings import Settings as FusionSettings
from trio.application.olap.tool_schemas import TERM_TOOLS, TOOL_NAMES
from trio.domain.gateway.errors import DatabaseNotConfiguredError
from trio.domain.gateway.samples import DEFAULT_PII_PATTERNS

logger = logging.getLogger(__name__)


class DatabaseConfig(BaseModel):
    """Database connection configuration.

    ``options`` reaches the adapter untouched. Common keys: ``pool_size``,
    ``pool_min_size``, ``ssl`` (PostgreSQL) and ``schema`` (the schema to
    introspect; defaults to ``public`` / the database name / ``dbo``). The ODBC
    adapter (``mssql``, ``sqlserver``, ``odbc``) also reads ``driver`` (ODBC
    driver name), ``connection_string`` (used verbatim instead of
    host/port/database), ``encrypt`` and ``trust_server_certificate`` (SQL
    Server TLS), ``extra`` (raw ``Key=Value;`` pairs appended to the
    connection string) and ``pool_recycle`` (seconds).
    """

    name: str
    type: str  # postgresql | mysql | mssql | odbc (see DatabaseFactory.get_supported_types)
    host: str = "localhost"
    port: int = 5432
    database: str
    username: str
    password: str = ""
    options: dict[str, Any] = Field(default_factory=dict)
    # Optional read-only credentials used ONLY by the raw SQL endpoint, so that
    # even a bypass of the command whitelist cannot mutate data. Leave empty to
    # reuse the main (read-write) connection.
    readonly_username: str = ""
    readonly_password: str = ""

    def readonly_config(self) -> dict[str, Any] | None:
        """Build an adapter config for the read-only connection, or None.

        Returns a copy of this config with the username/password swapped for the
        read-only credentials, or None when no read-only user is configured.
        """
        if not self.readonly_username:
            return None
        cfg = self.model_dump()
        cfg["name"] = f"{self.name}__readonly"
        cfg["username"] = self.readonly_username
        cfg["password"] = self.readonly_password
        return cfg


class PaginationConfig(BaseModel):
    """Pagination settings."""

    default_limit: int = 50
    max_limit: int = 1000


class ExportConfig(BaseModel):
    """Streaming export (``GET|POST /{table}/export``) settings.

    Export is read-only (it needs the ``read`` permission) and streams rows in
    batches straight from a server-side cursor, so it is safe to leave enabled
    in production. ``max_rows`` caps a single export (0 = unlimited) and
    ``statement_timeout_ms`` bounds the database statement (0 = driver default).
    """

    enabled: bool = True
    max_rows: int = 0
    batch_size: int = 5000
    statement_timeout_ms: int = 0


class RowRuleConfig(BaseModel):
    """One mandatory row condition attached to an API key."""

    column: str
    operator: str = "eq"
    # ``${tenant}`` / ``${username}`` are filled in from the calling key.
    value: Any = None


class ApiKeyConfig(BaseModel):
    """API Key configuration with permissions."""

    key: str
    name: str = "default"
    permissions: list[str] = Field(default_factory=lambda: ["read"])
    # Permissions: read, create, update, delete, query (for raw queries)
    # Or use "all" for full access

    # Row-level security. `tenant` and the key's `name` are what `${tenant}`
    # and `${username}` resolve to inside a rule's value.
    tenant: str | None = None
    roles: list[str] = Field(default_factory=list)
    # table name -> conditions ANDed onto every read of that table, and
    # enforced on writes. A caller cannot widen or drop them.
    row_filters: dict[str, list[RowRuleConfig]] = Field(default_factory=dict)

    @property
    def has_row_rules(self) -> bool:
        """Whether this key carries any row-level restriction."""
        return any(self.row_filters.values())


#: Signature algorithms an identity provider's token may use. Never "none" or
#: an HMAC: a shared secret would let anyone holding it mint a token.
OIDC_ALGORITHMS = ("RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512")


class OidcConfig(BaseModel):
    """Bearer tokens from an OpenID Connect provider, beside the API keys.

    The provider (Keycloak, Entra ID, Okta, ...) signs a JWT; trio checks the
    signature against the provider's published keys, the issuer, the audience
    and the expiry, and reads who the caller is from the claims. Nobody is
    stored here: what a caller may do comes from their roles.
    """

    issuer: str = Field(
        ..., min_length=1, description="The provider's issuer URL, as tokens carry it"
    )
    audience: str = Field(..., min_length=1, description="What a token must be issued for (aud)")
    #: Where the signing keys are; by default the provider's discovery document says.
    jwks_url: str | None = None
    algorithms: list[str] = Field(default_factory=lambda: ["RS256", "ES256"])
    leeway_seconds: int = Field(default=30, ge=0, le=300)
    #: Claims, by dotted path ("realm_access.roles" for Keycloak).
    username_claim: str = "preferred_username"
    tenant_claim: str | None = None
    roles_claim: str = "roles"
    #: role -> permissions, as an API key lists them; a caller gets the union.
    role_permissions: dict[str, list[str]] = Field(default_factory=dict)
    #: What any valid token may do, whatever its roles.
    default_permissions: list[str] = Field(default_factory=list)
    #: Row rules for every caller who signs in (``${tenant}``, ``${username}``
    #: from the claims), except one holding a role listed next.
    row_filters: dict[str, list[RowRuleConfig]] = Field(default_factory=dict)
    row_filter_exempt_roles: list[str] = Field(default_factory=list)
    #: This server's own URL, as MCP clients reach it, for the protected
    #: resource metadata they discover the provider by. Default: the request's.
    resource_url: str | None = None
    #: Scopes a client should ask the provider for.
    scopes: list[str] = Field(default_factory=list)
    #: The console's own client at the provider (public, PKCE, redirect URI
    #: ``<this server>/ui``). Set it and the console offers to sign in.
    console_client_id: str | None = None
    console_scopes: list[str] = Field(default_factory=lambda: ["openid"])

    @field_validator("algorithms")
    @classmethod
    def _asymmetric_only(cls, value: list[str]) -> list[str]:
        wrong = [a for a in value if a not in OIDC_ALGORITHMS]
        if wrong or not value:
            raise ValueError(
                f"auth.oidc.algorithms: {', '.join(wrong) or 'none given'} not allowed; "
                f"use one of {', '.join(OIDC_ALGORITHMS)}"
            )
        return value

    @field_validator("role_permissions", "default_permissions")
    @classmethod
    def _known_permissions(cls, value: Any) -> Any:
        listed = value.values() if isinstance(value, dict) else [value]
        known = {"read", "create", "update", "delete", "query", "audit", "admin", "all"}
        wrong = sorted({p for perms in listed for p in perms} - known)
        if wrong:
            raise ValueError(f"auth.oidc: unknown permission(s) {', '.join(wrong)}")
        return value


class AuthConfig(BaseModel):
    """Authentication configuration."""

    enabled: bool = False
    header_name: str = "X-API-Key"
    api_keys: list[ApiKeyConfig] = Field(default_factory=list)
    # Public endpoints that don't require auth (e.g., health check)
    public_paths: list[str] = Field(
        default_factory=lambda: ["/health", "/docs", "/redoc", "/openapi.json"]
    )
    # Bearer tokens from an identity provider; None = API keys only.
    oidc: OidcConfig | None = None
    # A file for the keys an administrator hands out from the console (kept as
    # digests). None = only the keys above; nothing can be handed out.
    key_store: str | None = None
    # The enriched OpenAPI spec exposes column descriptions and x-llm-context.
    # In production, startup is refused while "/openapi.json" is public unless
    # this is explicitly set to true (see validate_production_config).
    allow_public_openapi: bool = False


class MaskingConfig(BaseModel):
    """Column masking, keyed by the catalog's semantic types.

    Rules attach to a semantic type rather than a column name, so a newly
    discovered PII column is masked as soon as the catalog labels it.
    """

    enabled: bool = False
    # semantic type -> redact | partial | last4 | hash | null
    rules: dict[str, str] = Field(default_factory=dict)
    # role -> its own semantic-type rules, overriding `rules` entirely
    by_role: dict[str, dict[str, str]] = Field(default_factory=dict)
    # roles that see raw values
    exempt_roles: list[str] = Field(default_factory=list)
    # Keys the `hash` strategy, which refuses to run without one. Store it
    # apart from anything it masks: whoever holds it can re-derive the values.
    hash_secret: str | None = None

    @property
    def strategies(self) -> set[str]:
        """Every strategy this configuration asks for, from both rule sources.

        ``by_role`` is the one that gets forgotten — a support role's own rules
        live there and nowhere else.
        """
        used = set(self.rules.values())
        for rules in self.by_role.values():
            used |= set(rules.values())
        return used

    @model_validator(mode="after")
    def _hash_needs_a_key(self) -> "MaskingConfig":
        """Refuse a `hash` rule with no key, where the configuration is read.

        Checked here rather than where masking is applied, so it does not
        depend on the environment or on `auto_discover_tables` being on. An
        unkeyed digest of enumerable data is reversible, and the two ways of
        carrying on — skipping the rule, or hashing unkeyed — would both
        return readable PII while reporting that it was masked.

        Raises:
            ValueError: Naming the setting that is missing.
        """
        if self.enabled and "hash" in self.strategies and not self.hash_secret:
            raise ValueError(
                "masking uses the 'hash' strategy but masking.hash_secret is not set. "
                "An unkeyed digest of an email or a national id is reversible by "
                "enumeration, so there is no safe default. Set masking.hash_secret, "
                "or use redact/partial/last4/null."
            )
        return self


class AuditConfig(BaseModel):
    """Audit trail for data access."""

    enabled: bool = False
    # Appended to, never rewritten. Events also go to the `warp.audit` logger.
    file: str | None = None
    # Record the column names a caller filtered on (never the values).
    log_filter_columns: bool = True
    # Keep the words of a question asked in words (answer_question,
    # search_terms) in its usage record. Off: a question can name a person.
    question_text: bool = False


class CatalogConfig(BaseModel):
    """Catalog storage configuration."""

    storage_path: str = "./catalogs"
    default_format: str = "json"
    auto_cross_reference: bool = True
    auto_enrich_openapi: bool = True
    openapi_enrichment_lang: str = "en"
    # Inject real sample values from the database into the OpenAPI spec as
    # `example`/`examples`/`x-llm-context.examples`. Off by default: the spec
    # is often reachable without a key and samples may contain sensitive data.
    openapi_include_examples: bool = False


class AnalysisConfig(BaseModel):
    """Schema analysis configuration."""

    sample_limit: int = 5
    include_row_count: bool = True
    excluded_tables: list[str] = Field(default_factory=list)
    excluded_schemas: list[str] = Field(
        default_factory=lambda: ["information_schema", "pg_catalog"]
    )
    # Privacy: sending raw sample rows to a *cloud* LLM is opt-in. When False,
    # samples are still read for stats/catalog but not sent to cloud providers.
    share_samples_with_cloud_llm: bool = False
    # Mask PII-looking column values before sending samples to any LLM.
    mask_pii_samples: bool = True
    pii_column_patterns: list[str] = Field(default_factory=lambda: list(DEFAULT_PII_PATTERNS))


# Providers that send prompt data off the local machine to a third-party API.
# (Ollama runs locally and is intentionally excluded.)
CLOUD_PROVIDERS: frozenset[str] = frozenset({"openai", "anthropic", "gemini"})


class LLMSlot(BaseModel):
    """One model: the provider, the model name and how it is called."""

    provider: str = "openai"
    model: str = "gpt-4o-mini"
    api_key: str = ""
    base_url: str = ""
    temperature: float = 0.3
    max_tokens: int = 4096

    @property
    def is_cloud_provider(self) -> bool:
        """Whether prompts (and any sample data) leave the local machine."""
        return self.provider.lower() in CLOUD_PROVIDERS


LLM_SLOTS = ("reasoning", "non_reasoning", "embed", "rerank")


class LLMTriplet(BaseModel):
    """The models by job; a slot left empty uses ``default``.

    The pre-1.0 flat block (``llm: {provider: ..., model: ...}``) is still
    read: it becomes ``default``.
    """

    default: LLMSlot = Field(default_factory=LLMSlot)
    reasoning: LLMSlot | None = None
    non_reasoning: LLMSlot | None = None
    embed: LLMSlot | None = None
    rerank: LLMSlot | None = None
    language_prompts: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _flat_block_is_the_default(cls, data: Any) -> Any:
        if not isinstance(data, Mapping):
            return data
        flat = {k: v for k, v in data.items() if k in LLMSlot.model_fields}
        if not flat:
            return data
        if "default" in data:
            raise ValueError(
                f"llm mixes the old flat keys ({', '.join(sorted(flat))}) with 'default'; "
                "move them under llm.default"
            )
        rest = {k: v for k, v in data.items() if k not in flat}
        return {**rest, "default": flat}

    def resolve(self, slot: str) -> LLMSlot:
        """The model configured for ``slot`` (one of ``LLM_SLOTS``), else the default."""
        if slot not in LLM_SLOTS:
            raise ValueError(f"Unknown LLM slot '{slot}'. Known: {', '.join(LLM_SLOTS)}")
        return getattr(self, slot) or self.default


class I18nConfig(BaseModel):
    """Internationalization configuration."""

    default_language: str = "en"
    languages: list[str] = Field(default_factory=lambda: ["en"])
    fallback_language: str = "en"
    auto_translate: bool = True
    translation_strategy: str = "single"  # single | multi


class SettingsConfig(BaseModel):
    """Application settings."""

    auto_discover_tables: bool = True
    excluded_tables: list[str] = Field(default_factory=list)
    pagination: PaginationConfig = Field(default_factory=PaginationConfig)
    export: ExportConfig = Field(default_factory=ExportConfig)
    enable_raw_query: bool = False  # default off; opt-in only, refused in production
    raw_query_whitelist: list[str] = Field(default_factory=lambda: ["SELECT"])
    # Columns clients may never write (mass-assignment protection). The primary
    # key and auto-generated columns are always protected in addition to these.
    readonly_columns: list[str] = Field(default_factory=lambda: ["created_at", "updated_at"])
    api_prefix: str = "/api/v1"
    docs_url: str = "/docs"
    redoc_url: str = "/redoc"
    auth: AuthConfig = Field(default_factory=AuthConfig)
    catalog: CatalogConfig = Field(default_factory=CatalogConfig)
    masking: MaskingConfig = Field(default_factory=MaskingConfig)
    audit: AuditConfig = Field(default_factory=AuditConfig)
    analysis: AnalysisConfig = Field(default_factory=AnalysisConfig)
    llm: LLMTriplet = Field(default_factory=LLMTriplet)
    i18n: I18nConfig = Field(default_factory=I18nConfig)


class RemoteWarpConfig(BaseModel):
    """A Warp on another node, read over HTTP (source type ``warp-remote``).

    Empty ``url`` = none. ``databases`` empty = every database that Warp lists.
    """

    url: str = ""
    databases: list[str] = Field(default_factory=list)
    api_key: str = ""
    api_key_header: str = "X-API-Key"
    timeout: float = 30.0
    max_retries: int = 3
    backoff_factor: float = 2.0
    pool_size: int = 10
    pool_max_overflow: int = 5
    circuit_breaker_threshold: int = 5
    circuit_breaker_timeout: float = 60.0


_RATE_ITEM = r"\s*\d+\s*(?:/|per)\s*(?:\d+\s*)?(?:second|minute|hour|day|month|year)s?\s*"
_RATE_LIMIT = re.compile(rf"{_RATE_ITEM}(?:[;,|]{_RATE_ITEM})*", re.IGNORECASE)


class OlapConfig(BaseModel):
    """The analytics side: DuckDB, how much a query may pull in, cache, backups."""

    enabled: bool = True

    # Analytics store (DuckDB)
    database: str = ":memory:"
    memory_limit: str = "4GB"
    threads: int = 4
    # Lets DuckDB touch the filesystem/network; needed only for export backups.
    external_access: bool = False
    max_temp_directory_size: str = ""
    # Where queries too big for memory spill; empty is DuckDB's `.tmp` beside
    # the working directory. Set it when the root filesystem is read-only.
    temp_directory: str = ""
    max_ingest_rows: int = 0

    # Row budgets (see MaterializationPolicy)
    full_load_max_rows: int = 500_000
    slice_max_rows: int = 500_000
    slice_budget_rows: int = 2_000_000
    semi_join_max_keys: int = 50_000
    in_chunk_size: int = 1_000

    # "db.table" -> {watermark_column, key_columns}
    refresh: dict[str, dict[str, Any]] = Field(default_factory=dict)
    # "db.table" -> {measures: [...], dimensions: [...]}
    semantic_models: dict[str, dict[str, Any]] = Field(default_factory=dict)

    cache_ttl: int = 300
    cache_max_entries: int = 500
    rate_limit: str = "100/minute"
    # None = on outside production, off in production.
    debug_endpoints: bool | None = None

    backup_enabled: bool = False
    backup_interval: int = 3600
    backup_path: str = "/app/data/backups"
    backup_retention_days: int = 7

    # Joins checked against the declared foreign keys and the approved
    # catalog: advise reports a wrong join with the result, repair fixes it or
    # refuses the query.
    grounding: Literal["off", "advise", "repair"] = "off"
    # The term layer: catalog analysis writes business terms (one more model
    # call per table), and the ones a reviewer certified name the measures,
    # feed answer_question and are offered through search_terms and get_term.
    # Until a term is certified, on changes nothing a query returns.
    terms: Literal["on", "off"] = "on"

    remote: RemoteWarpConfig = Field(default_factory=RemoteWarpConfig)

    @field_validator("rate_limit")
    @classmethod
    def _rate_limit_parses(cls, value: str) -> str:
        # slowapi skips a limit it cannot parse and serves unthrottled.
        if not _RATE_LIMIT.fullmatch(value):
            raise ValueError(
                f"rate_limit {value!r} is not a limit such as '100/minute' or "
                f"'10 per second; 1000 per day'"
            )
        return value

    @field_validator("terms", mode="before")
    @classmethod
    def _yaml_booleans(cls, value: Any) -> Any:
        # YAML 1.1 reads a bare on/off as a boolean.
        return {True: "on", False: "off"}.get(value, value) if isinstance(value, bool) else value

    def to_fusion_settings(self, env: str = "development") -> FusionSettings:
        """The engine's own settings object, so its services need no change."""
        debug = "" if self.debug_endpoints is None else str(self.debug_endpoints).lower()
        return FusionSettings(
            env=env,
            warp_url=self.remote.url or "http://localhost:8000",
            warp_api_key=self.remote.api_key,
            warp_api_key_header=self.remote.api_key_header,
            warp_timeout=self.remote.timeout,
            warp_max_retries=self.remote.max_retries,
            warp_backoff_factor=self.remote.backoff_factor,
            database=self.database,
            memory_limit=self.memory_limit,
            threads=self.threads,
            external_access=self.external_access,
            max_temp_directory_size=self.max_temp_directory_size,
            temp_directory=self.temp_directory,
            max_ingest_rows=self.max_ingest_rows,
            full_load_max_rows=self.full_load_max_rows,
            slice_max_rows=self.slice_max_rows,
            slice_budget_rows=self.slice_budget_rows,
            semi_join_max_keys=self.semi_join_max_keys,
            in_chunk_size=self.in_chunk_size,
            refresh_config=json.dumps(self.refresh) if self.refresh else "",
            semantic_model=json.dumps(self.semantic_models) if self.semantic_models else "",
            cache_ttl=self.cache_ttl,
            cache_max_entries=self.cache_max_entries,
            rate_limit=self.rate_limit,
            debug_endpoints=debug,
            log_file="",
            circuit_breaker_threshold=self.remote.circuit_breaker_threshold,
            circuit_breaker_timeout=self.remote.circuit_breaker_timeout,
            pool_size=self.remote.pool_size,
            pool_max_overflow=self.remote.pool_max_overflow,
            backup_enabled=self.backup_enabled,
            backup_interval=self.backup_interval,
            backup_path=self.backup_path,
            backup_retention_days=self.backup_retention_days,
            grounding=self.grounding,
            extra={"grounding": self.grounding, "terms": self.terms},
        )


#: Tools that change state or expose internals; the analyst profile leaves them out.
MCP_ADMIN_ONLY_TOOLS = frozenset({"create_view", "refresh_view", "cache_stats"})


class McpConfig(BaseModel):
    """The curated MCP server (the OLAP tools over streamable HTTP and stdio)."""

    # analyst = read-only tools; admin = all of them.
    profile: Literal["analyst", "admin"] = "analyst"
    # Narrows the profile further, by tool name. An unknown name refuses startup.
    tools: list[str] | None = None
    # The API key (by ``name``) a stdio session acts as when auth is enabled.
    stdio_api_key_name: str | None = None
    http_path: str = "/mcp"

    @field_validator("http_path")
    @classmethod
    def _absolute_path(cls, value: str) -> str:
        if not value.startswith("/") or value == "/":
            raise ValueError("mcp.http_path must be an absolute path such as /mcp")
        return value.rstrip("/")

    @model_validator(mode="after")
    def _known_tools(self) -> "McpConfig":
        self.allowed_tools()
        return self

    def allowed_tools(self) -> frozenset[str]:
        """The tool names this server exposes.

        Raises:
            ValueError: ``tools`` names something that does not exist, or that
                the profile does not offer. Dropping it silently would leave an
                operator believing a tool is there (or is not).
        """
        offered = set(TOOL_NAMES)
        if self.profile == "analyst":
            offered -= MCP_ADMIN_ONLY_TOOLS
        if self.tools is None:
            return frozenset(offered)
        unknown = sorted(set(self.tools) - set(TOOL_NAMES))
        if unknown:
            raise ValueError(f"mcp.tools names unknown tools: {', '.join(unknown)}")
        withheld = sorted(set(self.tools) - offered)
        if withheld:
            raise ValueError(
                f"mcp.tools asks for {', '.join(withheld)}, which the '{self.profile}' "
                "profile does not offer; use profile: admin"
            )
        return frozenset(self.tools)


class Settings(BaseModel):
    """Main configuration container."""

    databases: list[DatabaseConfig] = Field(default_factory=list)
    settings: SettingsConfig = Field(default_factory=SettingsConfig)
    olap: OlapConfig = Field(default_factory=OlapConfig)
    mcp: McpConfig = Field(default_factory=McpConfig)

    @model_validator(mode="after")
    def _term_tools_need_terms(self) -> "Settings":
        self.offered_tools()
        return self

    def offered_tools(self) -> frozenset[str]:
        """The MCP tools to expose: ``mcp``'s choice, less the term layer's when it is off.

        Raises:
            ValueError: ``mcp.tools`` asks for a term tool by name while
                ``olap.terms`` is off; it would be listed and answer nothing.
        """
        allowed = self.mcp.allowed_tools()
        if self.olap.terms == "on":
            return allowed
        asked = sorted(set(self.mcp.tools or ()) & TERM_TOOLS)
        if asked:
            raise ValueError(f"mcp.tools asks for {', '.join(asked)}, which need olap.terms: on")
        return allowed - TERM_TOOLS

    def database(self, name: str) -> DatabaseConfig:
        """Return the configuration of the database called `name`.

        Raises:
            DatabaseNotConfiguredError: If no database with that name is configured.
        """
        for db in self.databases:
            if db.name == name:
                return db
        raise DatabaseNotConfiguredError(name, [db.name for db in self.databases])


# -- pre-1.0 environment variables ------------------------------------------------

#: Fusion's variables -> the ``olap`` field each one set.
LEGACY_OLAP_ENV: dict[str, str] = {
    "FUSION_DATABASE": "database",
    "FUSION_MEMORY_LIMIT": "memory_limit",
    "FUSION_THREADS": "threads",
    "FUSION_DUCKDB_EXTERNAL_ACCESS": "external_access",
    "FUSION_MAX_TEMP_DIRECTORY_SIZE": "max_temp_directory_size",
    "FUSION_MAX_INGEST_ROWS": "max_ingest_rows",
    "FUSION_FULL_LOAD_MAX_ROWS": "full_load_max_rows",
    "FUSION_SLICE_MAX_ROWS": "slice_max_rows",
    "FUSION_SLICE_BUDGET_ROWS": "slice_budget_rows",
    "FUSION_SEMI_JOIN_MAX_KEYS": "semi_join_max_keys",
    "FUSION_IN_CHUNK_SIZE": "in_chunk_size",
    "FUSION_REFRESH_CONFIG": "refresh",
    "FUSION_SEMANTIC_MODEL": "semantic_models",
    "FUSION_CACHE_TTL": "cache_ttl",
    "FUSION_CACHE_MAX_ENTRIES": "cache_max_entries",
    "FUSION_RATE_LIMIT": "rate_limit",
    "FUSION_DEBUG_ENDPOINTS": "debug_endpoints",
    "FUSION_BACKUP_ENABLED": "backup_enabled",
    "FUSION_BACKUP_INTERVAL": "backup_interval",
    "FUSION_BACKUP_PATH": "backup_path",
    "FUSION_BACKUP_RETENTION_DAYS": "backup_retention_days",
}

#: Fusion's Warp-over-HTTP variables -> the ``olap.remote`` field.
LEGACY_REMOTE_ENV: dict[str, str] = {
    "WARP_URL": "url",
    "WARP_API_KEY": "api_key",
    "FUSION_WARP_API_KEY_HEADER": "api_key_header",
    "WARP_TIMEOUT": "timeout",
    "WARP_MAX_RETRIES": "max_retries",
    "WARP_BACKOFF_FACTOR": "backoff_factor",
    "FUSION_POOL_SIZE": "pool_size",
    "FUSION_POOL_MAX_OVERFLOW": "pool_max_overflow",
    "FUSION_CIRCUIT_BREAKER_THRESHOLD": "circuit_breaker_threshold",
    "FUSION_CIRCUIT_BREAKER_TIMEOUT": "circuit_breaker_timeout",
}

#: Process-level variables: the current name -> Fusion's old one (see RuntimeEnv).
LEGACY_PROCESS_ENV: dict[str, str] = {
    "APP_ENV": "FUSION_ENV",
    "API_HOST": "FUSION_HOST",
    "API_PORT": "FUSION_PORT",
    "CORS_ORIGINS": "FUSION_CORS_ORIGINS",
    "LOG_LEVEL": "FUSION_LOG_LEVEL",
    "LOG_FORMAT": "FUSION_LOG_FORMAT",
}

LEGACY_API_KEY_NAME = "fusion"


def _deprecated(name: str, instead: str) -> None:
    logger.warning("%s is deprecated and will stop being read; use %s instead", name, instead)


def legacy_env_overlay(settings: Settings, environ: Mapping[str, str]) -> Settings:
    """Apply the pre-1.0 ``FUSION_*``/``WARP_*`` variables on top of ``settings``.

    Every variable found is applied and logged as deprecated. A value that
    does not parse is logged and skipped, as Fusion did. The process-level
    ones (``FUSION_ENV``, ``FUSION_PORT``, ...) are read by ``RuntimeEnv``.
    """
    olap: dict[str, Any] = {}
    for name, target in LEGACY_OLAP_ENV.items():
        raw = environ.get(name, "")
        if not raw.strip():
            continue
        try:
            olap[target] = json.loads(raw) if target in ("refresh", "semantic_models") else raw
        except ValueError as e:
            logger.error("%s is not valid JSON, ignoring it: %s", name, e)
            continue
        _deprecated(name, f"olap.{target} in the config file")
    remote: dict[str, Any] = {}
    for name, target in LEGACY_REMOTE_ENV.items():
        if environ.get(name, "").strip():
            remote[target] = environ[name]
            _deprecated(name, f"olap.remote.{target} in the config file")
    if "FUSION_LOG_FILE" in environ:
        logger.warning("FUSION_LOG_FILE is no longer read: logs go to the process output")

    data = settings.model_dump()
    data["olap"] = {**data["olap"], **olap, "remote": {**data["olap"]["remote"], **remote}}
    api_key = environ.get("FUSION_API_KEY", "").strip()
    if api_key:
        # Fusion's single key had the run of the whole API.
        _deprecated("FUSION_API_KEY", "an entry in settings.auth.api_keys")
        auth = data["settings"]["auth"]
        auth["enabled"] = True
        auth["api_keys"] = [
            *auth["api_keys"],
            {"key": api_key, "name": LEGACY_API_KEY_NAME, "permissions": ["all"]},
        ]
    return Settings.model_validate(data)


_SECRET_KEY = re.compile(r"password|secret|api_key|token|connection_string|^key$", re.IGNORECASE)
MASK = "********"


def masked(value: Any, key: str = "") -> Any:
    """``value`` (a ``model_dump``) with every secret replaced, for display."""
    if isinstance(value, Mapping):
        return {k: masked(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [masked(item, key) for item in value]
    if _SECRET_KEY.search(key) and isinstance(value, str) and value:
        return MASK
    return value


@dataclass(frozen=True)
class RuntimeEnv:
    """Process-level settings that come from the environment, not the YAML config."""

    app_env: str = "development"
    log_level: str = "INFO"
    log_format: str = ""  # "json" | "colored"; empty -> json in production, colored otherwise
    cors_origins: tuple[str, ...] = ("*",)
    config_path: str | None = None
    api_host: str = "0.0.0.0"  # noqa: S104 - a server binds all interfaces by default
    api_port: int = 8000
    api_workers: int = 1

    @property
    def is_production(self) -> bool:
        """Whether `APP_ENV=production`."""
        return self.app_env == "production"

    @property
    def log_json(self) -> bool:
        """Whether logs are emitted as JSON lines."""
        return self.log_format == "json" or (not self.log_format and self.is_production)

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> "RuntimeEnv":
        """Read the well-known `APP_ENV`, `LOG_*`, `CORS_ORIGINS`, `CONFIG_PATH`, `API_*` variables.

        Fusion's names for the same things (``FUSION_ENV``, ``FUSION_PORT``,
        ...) are still read when the current one is unset, and logged as deprecated.
        """
        env = os.environ if environ is None else environ

        def get(name: str, default: str) -> str:
            legacy = LEGACY_PROCESS_ENV.get(name, "")
            if name not in env and legacy in env:
                _deprecated(legacy, name)
                return env[legacy]
            return env.get(name, default)

        origins = tuple(o.strip() for o in get("CORS_ORIGINS", "*").split(",") if o.strip())
        return cls(
            app_env=get("APP_ENV", "development"),
            log_level=get("LOG_LEVEL", "INFO").upper(),
            log_format=get("LOG_FORMAT", ""),
            cors_origins=origins or ("*",),
            config_path=env.get("CONFIG_PATH") or None,
            api_host=get("API_HOST", "0.0.0.0"),  # noqa: S104
            api_port=int(get("API_PORT", "8000")),
            api_workers=int(env.get("API_WORKERS", "1")),
        )


# API keys that must never be accepted in production (placeholders/examples).
PLACEHOLDER_API_KEYS = frozenset(
    {
        "changeme",
        "change-me",
        "your-api-key",
        "your-secure-api-key-here-change-in-production",
    }
)

#: Below this a masking key is a passphrase somebody chose, which is itself
#: dictionary-attackable — the hole the key exists to close.
MIN_HASH_SECRET_LENGTH = 32


def validate_production_config(
    settings: Settings,
    app_env: str,
    cors_origins: list[str] | None = None,
) -> list[str]:
    """Check security-sensitive configuration for production deployments.

    Returns a list of human-readable violation messages. The list is only
    populated when ``app_env == "production"``; in any other environment the
    same options are legitimate defaults and no violations are reported.

    The caller is expected to refuse startup (fail-safe) when the returned
    list is non-empty.

    Args:
        settings: Loaded application settings.
        app_env: The current ``APP_ENV`` value.
        cors_origins: The effective CORS allowlist (e.g. from ``CORS_ORIGINS``).

    Returns:
        A list of violation messages (empty when the configuration is safe).
    """
    violations: list[str] = []

    if app_env != "production":
        return violations

    cfg = settings.settings

    if not cfg.auth.enabled:
        violations.append(
            "auth.enabled is false: every endpoint would be public. "
            "Enable authentication for production."
        )

    if cfg.enable_raw_query:
        violations.append(
            "settings.enable_raw_query is true: the raw SQL endpoint would be "
            "exposed. Set enable_raw_query: false for production."
        )

    if cors_origins and any(origin.strip() == "*" for origin in cors_origins):
        violations.append(
            "CORS_ORIGINS allows '*': any origin could call the API. "
            "Set CORS_ORIGINS to an explicit, comma-separated allowlist."
        )

    for key in cfg.auth.api_keys:
        if key.key.strip().lower() in PLACEHOLDER_API_KEYS:
            violations.append(
                f"auth.api_keys entry '{key.name}' is a placeholder. Set a strong, unique key."
            )

    oidc = cfg.auth.oidc
    if oidc is not None:
        for name, url in (("issuer", oidc.issuer), ("jwks_url", oidc.jwks_url)):
            if url and not url.lower().startswith("https://"):
                violations.append(
                    f"auth.oidc.{name} is not https: the signing keys could be swapped "
                    f"on the way. Use the provider's https URL."
                )
        if not cfg.auth.enabled:
            violations.append(
                "auth.oidc is set while auth.enabled is false: no token would be checked."
            )

    if not cfg.auth.enabled and any(key.has_row_rules for key in cfg.auth.api_keys):
        violations.append(
            "auth.enabled is false while API keys carry row_filters: nobody would "
            "be identified, so the row rules would not apply to anything. Enable "
            "authentication or remove the row filters."
        )

    # A *missing* key is refused when the configuration is parsed, by
    # MaskingConfig itself. What is left for production is whether the key that
    # is there is strong enough to be worth having.
    if cfg.masking.enabled and "hash" in cfg.masking.strategies:
        secret = cfg.masking.hash_secret or ""
        if secret and len(secret) < MIN_HASH_SECRET_LENGTH:
            violations.append(
                f"masking.hash_secret is shorter than {MIN_HASH_SECRET_LENGTH} "
                f"characters: a guessable key is the same exposure as no key. "
                f"Use a generated secret."
            )

    openapi_public = any(
        p.rstrip("/") in ("/openapi.json", "/openapi", "/") for p in cfg.auth.public_paths
    )
    if openapi_public and not cfg.auth.allow_public_openapi:
        violations.append(
            "auth.public_paths exposes /openapi.json: the enriched OpenAPI spec "
            "(column descriptions, x-llm-context) would be readable without an API "
            "key. Remove it from auth.public_paths, or set "
            "auth.allow_public_openapi: true to accept this explicitly."
        )

    return violations
