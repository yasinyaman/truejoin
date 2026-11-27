"""The one Settings model: olap/mcp blocks, the LLM triplet, legacy env, ${file:...}."""

import logging

import pytest
from pydantic import ValidationError

from truejoin.adapters.outbound.yaml_config import interpolate_env_vars, load_config
from truejoin.application.settings import (
    LEGACY_OLAP_ENV,
    LEGACY_REMOTE_ENV,
    MASK,
    ApiKeyConfig,
    AuthConfig,
    LLMTriplet,
    McpConfig,
    OlapConfig,
    RuntimeEnv,
    Settings,
    SettingsConfig,
    legacy_env_overlay,
    masked,
    validate_production_config,
)
from truejoin.domain.olap.models import RefreshSpec


class TestOlapConfig:
    def test_defaults_match_the_engine_defaults(self):
        from truejoin.application.olap.settings import Settings as FusionSettings

        mapped, plain = OlapConfig().to_fusion_settings(), FusionSettings()
        for name in (
            "database",
            "memory_limit",
            "threads",
            "cache_ttl",
            "rate_limit",
            "refresh_interval",
        ):
            assert getattr(mapped, name) == getattr(plain, name)
        assert mapped.policy() == plain.policy()
        assert mapped.warp_http_defaults() == plain.warp_http_defaults()

    def test_every_field_reaches_the_engine_settings(self):
        olap = OlapConfig(
            database="/data/olap.duckdb",
            threads=2,
            slice_max_rows=7,
            refresh_interval=900,
            refresh={"db.orders": {"watermark_column": "updated_at", "key_columns": ["id"]}},
            semantic_models={"db.orders": {"measures": ["amount"], "dimensions": ["status"]}},
            backup_enabled=True,
            debug_endpoints=True,
            grounding="advise",
            terms="on",
            temp_directory="/app/data/tmp",
            remote={"url": "http://warp:8000", "timeout": 5},
        )
        mapped = olap.to_fusion_settings("production")
        assert mapped.is_production() and mapped.debug_endpoints_enabled()
        assert (mapped.database, mapped.threads) == ("/data/olap.duckdb", 2)
        assert mapped.temp_directory == "/app/data/tmp"
        assert mapped.policy().slice_max_rows == 7
        assert mapped.refresh_interval == 900
        assert mapped.refresh_specs() == {
            "db.orders": RefreshSpec(watermark_column="updated_at", key_columns=("id",))
        }
        assert [m.name for m in mapped.semantic_models()["db.orders"].measures] == ["*", "amount"]
        assert mapped.backup_enabled
        assert mapped.warp_url == "http://warp:8000" and mapped.warp_timeout == 5
        assert mapped.extra == {"grounding": "advise", "terms": "on"}
        assert mapped.grounding == "advise"

    def test_debug_endpoints_follow_the_environment_unless_set(self):
        assert OlapConfig().to_fusion_settings("development").debug_endpoints_enabled()
        assert not OlapConfig().to_fusion_settings("production").debug_endpoints_enabled()
        off = OlapConfig(debug_endpoints=False).to_fusion_settings("development")
        assert not off.debug_endpoints_enabled()

    def test_grounding_and_terms_are_closed_sets(self):
        with pytest.raises(ValidationError):
            OlapConfig(grounding="lenient")
        # YAML reads a bare on/off as a boolean.
        assert OlapConfig(terms=True).terms == "on"
        assert OlapConfig(terms=False).terms == "off"


class TestLLMTriplet:
    def test_an_empty_slot_falls_back_to_the_default(self):
        llm = LLMTriplet(default={"model": "small"}, reasoning={"model": "big"})
        assert llm.resolve("reasoning").model == "big"
        for slot in ("non_reasoning", "embed", "rerank"):
            assert llm.resolve(slot).model == "small"
        with pytest.raises(ValueError, match="Unknown LLM slot"):
            llm.resolve("vision")

    def test_the_old_flat_block_becomes_the_default(self):
        cfg = SettingsConfig(
            llm={"provider": "ollama", "model": "llama3", "language_prompts": {"tr": "Türkçe"}}
        )
        assert cfg.llm.default.provider == "ollama"
        assert cfg.llm.resolve("reasoning").model == "llama3"
        assert cfg.llm.language_prompts == {"tr": "Türkçe"}

    def test_mixing_the_flat_block_with_default_is_refused(self):
        with pytest.raises(ValidationError, match="llm.default"):
            LLMTriplet(provider="ollama", default={"model": "x"})


class TestMcpConfig:
    def test_defaults(self):
        mcp = McpConfig()
        assert (mcp.profile, mcp.tools, mcp.stdio_api_key_name, mcp.http_path) == (
            "analyst",
            None,
            None,
            "/mcp",
        )

    @pytest.mark.parametrize("path", ["mcp", "/", ""])
    def test_http_path_must_be_absolute(self, path):
        with pytest.raises(ValidationError):
            McpConfig(http_path=path)

    def test_unknown_profile_is_refused(self):
        with pytest.raises(ValidationError):
            McpConfig(profile="root")


class TestTermTools:
    TERMS = {"search_terms", "get_term", "answer_question"}

    def test_they_are_offered_unless_the_term_layer_is_turned_off(self):
        assert Settings().olap.terms == "on"
        assert Settings().offered_tools() >= self.TERMS
        off = Settings(olap={"terms": "off"}).offered_tools()
        assert not self.TERMS & off and "query_data" in off

    def test_asking_for_one_by_name_while_it_is_off_is_an_error_not_a_dead_tool(self):
        with pytest.raises(ValidationError, match="answer_question, which need olap.terms: on"):
            Settings(olap={"terms": "off"}, mcp={"tools": ["query_data", "answer_question"]})
        chosen = Settings(mcp={"tools": ["query_data", "answer_question"]})
        assert chosen.offered_tools() == {"query_data", "answer_question"}


class TestFileInterpolation:
    def test_reads_a_secret_file_without_the_trailing_newline(self, tmp_path):
        secret = tmp_path / "db_pass"
        secret.write_text("s3cret\n")
        assert interpolate_env_vars({"password": f"${{file:{secret}}}"}) == {"password": "s3cret"}

    def test_a_missing_secret_file_is_an_error_not_an_empty_password(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            interpolate_env_vars(f"${{file:{tmp_path / 'absent'}}}")

    def test_load_config_reads_the_new_blocks(self, tmp_path, monkeypatch):
        (tmp_path / "key").write_text("k" * 40)
        monkeypatch.setenv("OLAP_THREADS", "3")
        config = tmp_path / "config.yaml"
        config.write_text(
            f"""
databases: []
settings:
  auth:
    enabled: true
    api_keys:
      - key: ${{file:{tmp_path / "key"}}}
        name: analyst
        permissions: [query]
olap:
  threads: ${{OLAP_THREADS:4}}
  terms: off
mcp:
  profile: admin
  http_path: /tools/mcp
"""
        )
        settings = load_config(str(config))
        assert settings.settings.auth.api_keys[0].key == "k" * 40
        assert settings.olap.threads == 3 and settings.olap.terms == "off"
        assert (settings.mcp.profile, settings.mcp.http_path) == ("admin", "/tools/mcp")


class TestLegacyEnvOverlay:
    def test_no_legacy_variables_changes_nothing(self, caplog):
        settings = Settings()
        with caplog.at_level(logging.WARNING):
            assert legacy_env_overlay(settings, {"PATH": "/bin"}) == settings
        assert caplog.records == []

    def test_every_olap_variable_is_applied_and_logged(self, caplog):
        values = {
            "refresh": '{"db.t": {"watermark_column": "ts"}}',
            "semantic_models": '{"db.t": {"measures": ["x"]}}',
            "database": "/data/f.duckdb",
            "memory_limit": "1GB",
            "max_temp_directory_size": "2GB",
            "rate_limit": "5/minute",
            "backup_path": "/b",
            "external_access": "true",
            "debug_endpoints": "false",
            "backup_enabled": "true",
        }
        environ = {name: values.get(field, "9") for name, field in LEGACY_OLAP_ENV.items()}
        with caplog.at_level(logging.WARNING):
            olap = legacy_env_overlay(Settings(), environ).olap
        assert olap.threads == 9 and olap.slice_budget_rows == 9 and olap.cache_ttl == 9
        assert olap.external_access is True and olap.debug_endpoints is False
        assert olap.backup_enabled is True and olap.backup_retention_days == 9
        assert olap.refresh == {"db.t": {"watermark_column": "ts"}}
        assert olap.semantic_models == {"db.t": {"measures": ["x"]}}
        changed = olap.model_dump(exclude={"remote"})
        defaults = OlapConfig().model_dump(exclude={"remote"})
        # Settings born after the merge have no pre-1.0 variable.
        untouched = {
            "enabled",
            "grounding",
            "terms",
            "temp_directory",
            "unread_questions",
            "refresh_interval",
            "assistant",
        }
        assert {k for k in changed if changed[k] == defaults[k]} == untouched
        logged = " ".join(r.getMessage() for r in caplog.records)
        assert all(f"{name} is deprecated" in logged for name in LEGACY_OLAP_ENV)

    def test_every_remote_variable_is_applied(self, caplog):
        environ = dict.fromkeys(LEGACY_REMOTE_ENV, "9")
        environ |= {"WARP_URL": "http://warp:8000", "WARP_API_KEY": "k"}
        with caplog.at_level(logging.WARNING):
            remote = legacy_env_overlay(Settings(), environ).olap.remote
        assert remote.url == "http://warp:8000" and remote.api_key == "k"
        assert remote.api_key_header == "9" and remote.timeout == 9.0 and remote.pool_size == 9
        assert remote.model_dump(exclude={"databases"}).keys() == set(LEGACY_REMOTE_ENV.values())
        assert len(caplog.records) == len(LEGACY_REMOTE_ENV)

    def test_the_single_api_key_becomes_a_full_access_key(self):
        settings = legacy_env_overlay(Settings(), {"FUSION_API_KEY": "old-key"})
        auth = settings.settings.auth
        assert auth.enabled
        assert [(k.key, k.name, k.permissions) for k in auth.api_keys] == [
            ("old-key", "fusion", ["all"])
        ]

    def test_malformed_json_is_logged_and_skipped(self, caplog):
        with caplog.at_level(logging.ERROR):
            olap = legacy_env_overlay(Settings(), {"FUSION_REFRESH_CONFIG": "{nope"}).olap
        assert olap.refresh == {}
        assert "FUSION_REFRESH_CONFIG is not valid JSON" in caplog.text

    def test_a_value_of_the_wrong_type_is_an_error(self):
        with pytest.raises(ValidationError):
            legacy_env_overlay(Settings(), {"FUSION_THREADS": "many"})

    def test_process_level_names_are_read_by_runtime_env(self, caplog):
        environ = {
            "FUSION_ENV": "production",
            "FUSION_HOST": "127.0.0.1",
            "FUSION_PORT": "9000",
            "FUSION_CORS_ORIGINS": "https://a,https://b",
            "FUSION_LOG_LEVEL": "debug",
            "FUSION_LOG_FORMAT": "json",
        }
        with caplog.at_level(logging.WARNING):
            env = RuntimeEnv.from_environ(environ)
        assert (env.app_env, env.api_host, env.api_port) == ("production", "127.0.0.1", 9000)
        assert env.cors_origins == ("https://a", "https://b")
        assert (env.log_level, env.log_format) == ("DEBUG", "json")
        assert len(caplog.records) == 6

    def test_the_current_name_wins_over_the_legacy_one(self, caplog):
        with caplog.at_level(logging.WARNING):
            env = RuntimeEnv.from_environ({"API_PORT": "8000", "FUSION_PORT": "9000"})
        assert env.api_port == 8000 and caplog.records == []


class TestProductionRulesFromFusion:
    def _settings(self, key: str) -> Settings:
        auth = AuthConfig(
            enabled=True, public_paths=["/health"], api_keys=[ApiKeyConfig(key=key, name="svc")]
        )
        return Settings(settings=SettingsConfig(auth=auth))

    @pytest.mark.parametrize("key", ["changeme", "your-api-key", "Change-Me"])
    def test_a_placeholder_api_key_is_refused_in_production(self, key):
        found = validate_production_config(self._settings(key), "production", ["https://a"])
        assert found == ["auth.api_keys entry 'svc' is a placeholder. Set a strong, unique key."]

    def test_a_placeholder_is_fine_outside_production(self):
        assert validate_production_config(self._settings("changeme"), "development") == []

    def test_a_real_key_passes(self):
        assert (
            validate_production_config(self._settings("k" * 40), "production", ["https://a"]) == []
        )


class TestMasked:
    def test_secrets_are_hidden_and_the_rest_is_kept(self):
        settings = Settings(
            databases=[
                {
                    "name": "db",
                    "type": "postgresql",
                    "database": "app",
                    "username": "u",
                    "password": "p",
                    "readonly_password": "",
                    "options": {"connection_string": "Driver=x;PWD=y"},
                }
            ],
            settings={
                "auth": {"api_keys": [{"key": "abc", "name": "svc"}]},
                "masking": {"hash_secret": "h" * 40},
                "llm": {"api_key": "sk-1"},
            },
            olap={"remote": {"api_key": "remote-key"}},
        )
        shown = masked(settings.model_dump())
        db = shown["databases"][0]
        assert db["password"] == MASK and db["username"] == "u"
        assert db["readonly_password"] == ""  # nothing to hide, and "set or not" stays visible
        assert db["options"]["connection_string"] == MASK
        assert shown["settings"]["auth"]["api_keys"][0] == {
            **shown["settings"]["auth"]["api_keys"][0],
            "key": MASK,
            "name": "svc",
        }
        assert shown["settings"]["masking"]["hash_secret"] == MASK
        assert shown["settings"]["llm"]["default"]["api_key"] == MASK
        assert shown["olap"]["remote"]["api_key"] == MASK
        assert shown["settings"]["auth"]["header_name"] == "X-API-Key"


class TestRateLimit:
    @pytest.mark.parametrize("limit", ["2/min", "", "three per minute", "5 per minut"])
    def test_a_limit_slowapi_would_skip_is_refused_at_load(self, limit):
        with pytest.raises(ValidationError, match="rate_limit"):
            OlapConfig(rate_limit=limit)

    @pytest.mark.parametrize(
        "limit", ["100/minute", "2/second", "10 per second; 1000 per day", "5 per 2 minutes"]
    )
    def test_what_is_accepted_is_what_slowapi_reads(self, limit):
        from limits import parse_many

        assert OlapConfig(rate_limit=limit).rate_limit == limit
        assert parse_many(limit)


class TestUnreadQuestions:
    def test_a_model_writes_the_sql_unless_refused_or_none_is_usable(self, monkeypatch):
        from truejoin.bootstrap import build_olap

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)

        assert OlapConfig().unread_questions == "sql"
        local = LLMTriplet(default={"provider": "ollama", "model": "qwen2.5-coder:7b"})
        cloud = LLMTriplet(default={"provider": "openai", "model": "gpt", "api_key": "sk-test"})
        # The default, with no key for the default provider: refused, and the server starts.
        keyless = LLMTriplet(default={"provider": "openai", "model": "gpt"})
        cases = (
            ("refuse", local, False, True),
            ("sql", local, True, True),
            ("sql", cloud, True, False),
            ("sql", keyless, False, True),
        )
        for choice, llm, wired, values in cases:
            settings = Settings(
                settings=SettingsConfig(llm=llm),
                olap=OlapConfig(unread_questions=choice, threads=1),
            )
            fusion = build_olap(settings, gateway_factory=None)  # type: ignore[arg-type]
            try:
                assert (fusion.tools.write_sql is not None) is wired
                # Samples go to a cloud model only when analysis may send them.
                assert fusion.tools.values_to_model is values
            finally:
                fusion.close()
        with pytest.raises(ValidationError):
            OlapConfig(unread_questions="guess")


class TestReaderSchema:
    """The readers look where the gateway looks: an Oracle schema is the session user."""

    def test_the_gateways_resolved_schema_wins(self):
        from types import SimpleNamespace

        from truejoin.application.settings import DatabaseConfig
        from truejoin.bootstrap import reader_schema

        config = DatabaseConfig(name="o", type="oracle", database="FREEPDB1", username="warp")
        assert reader_schema(SimpleNamespace(introspection_schema="WARP"), config) == "WARP"
        # Without one, the dialect's default (Oracle folds the configured name).
        assert reader_schema(SimpleNamespace(), config) == "FREEPDB1"
        pg = DatabaseConfig(name="p", type="postgresql", database="shop", username="u")
        assert reader_schema(SimpleNamespace(), pg) == "public"


class TestSqlFingerprint:
    def settings(self, **audit):
        return Settings(settings={"audit": {"enabled": True, **audit}})

    def test_off_unless_asked(self):
        from truejoin.bootstrap import sql_fingerprinter

        assert sql_fingerprinter(self.settings()) is None
        assert sql_fingerprinter(Settings(settings={"audit": {"sql_fingerprint": True}})) is None

    def test_on_it_matches_across_values_and_engines_read_their_own_way(self, monkeypatch):
        from truejoin.bootstrap import sql_fingerprinter

        monkeypatch.setenv("TRUEJOIN_SECRET_KEY", "k" * 44)
        fp = sql_fingerprinter(self.settings(sql_fingerprint=True))
        assert fp("SELECT * FROM t WHERE a = 'x'", "duckdb") == fp(
            "SELECT * FROM t WHERE a = 'y'", "duckdb"
        )
        assert fp('SELECT * FROM t WHERE a = "x"', "mariadb") == fp(
            'SELECT * FROM t WHERE a = "y"', "mysql"
        )
        monkeypatch.setenv("TRUEJOIN_SECRET_KEY", "z" * 44)
        other = sql_fingerprinter(self.settings(sql_fingerprint=True))
        assert other("SELECT 1", "duckdb") != fp("SELECT 1", "duckdb")

    def test_production_refuses_it_without_the_secret_key(self, monkeypatch):
        from truejoin.application.settings import validate_production_config

        monkeypatch.delenv("TRUEJOIN_SECRET_KEY", raising=False)
        violations = validate_production_config(self.settings(sql_fingerprint=True), "production")
        assert any("audit.sql_fingerprint needs TRUEJOIN_SECRET_KEY" in v for v in violations)


class TestAssistantSetting:
    LOCAL = {"default": {"provider": "ollama", "model": "qwen2.5:7b"}}
    CLOUD = {"default": {"provider": "openai", "model": "gpt", "api_key": "sk-test"}}

    def settings(self, llm: dict, share: bool = False, masking: dict | None = None, **assistant):
        return Settings(
            settings={
                "llm": llm,
                "analysis": {"share_samples_with_cloud_llm": share},
                "masking": masking or {},
            },
            olap={"threads": 1, "assistant": {"enabled": True, **assistant}},
        )

    def test_it_is_off_until_turned_on(self):
        assistant = Settings().olap.assistant
        assert assistant.enabled is False
        assert (assistant.slot, assistant.max_steps, assistant.timeout) == ("reasoning", 8, 120)
        assert (assistant.rate_limit, assistant.history_turns, assistant.max_concurrent) == (
            "10/minute",
            6,
            4,
        )

    def test_a_cloud_model_needs_the_word_that_rows_may_be_sent(self):
        with pytest.raises(ValidationError, match="share_samples_with_cloud_llm: true"):
            self.settings(self.CLOUD)
        # The default model is a cloud one, so turning it on alone refuses too.
        with pytest.raises(ValidationError, match=r"cloud provider \(openai\)"):
            Settings(olap={"assistant": {"enabled": True}})
        assert self.settings(self.CLOUD, share=True).olap.assistant.enabled
        assert self.settings(self.LOCAL).olap.assistant.enabled

    def test_it_is_the_slots_model_that_is_judged(self):
        mixed = {**self.CLOUD, "non_reasoning": {"provider": "ollama", "model": "m"}}
        assert self.settings(mixed, slot="non_reasoning").olap.assistant.slot == "non_reasoning"
        with pytest.raises(ValidationError, match="cloud provider"):
            self.settings(mixed, slot="reasoning")

    @pytest.mark.parametrize(
        "bad",
        [
            {"max_steps": 0},
            {"max_steps": 31},
            {"timeout": 5},
            {"history_turns": -1},
            {"max_concurrent": 0},
            {"rate_limit": "often"},
            {"slot": "embed"},
        ],
    )
    def test_limits_out_of_range_are_refused(self, bad):
        with pytest.raises(ValidationError):
            self.settings(self.LOCAL, **bad)

    def test_off_there_is_no_assistant_and_it_says_so(self):
        from truejoin.bootstrap import build_olap

        fusion = build_olap(Settings(olap={"threads": 1}), gateway_factory=None)  # type: ignore[arg-type]
        try:
            assert fusion.assistant is None and "is off" in fusion.assistant_unavailable
        finally:
            fusion.close()

    def test_on_it_is_built_from_the_slots_model_and_the_limits(self):
        from truejoin.bootstrap import build_olap

        settings = self.settings(self.LOCAL, max_steps=3, timeout=30, history_turns=2)
        fusion = build_olap(settings, gateway_factory=None)  # type: ignore[arg-type]
        try:
            assistant = fusion.assistant
            assert assistant is not None and fusion.assistant_unavailable == ""
            assert (assistant.provider, assistant.model) == ("ollama", "qwen2.5:7b")
            assert (assistant.limits.max_steps, assistant.limits.timeout) == (3, 30.0)
            assert assistant.limits.history_turns == 2
            assert "answer_question" in assistant.tool_names
            assert assistant.mask_everyone is False  # the model is on this server
        finally:
            fusion.close()

    def test_with_the_term_layer_off_its_tools_are_not_the_assistants(self):
        from truejoin.bootstrap import build_olap

        settings = self.settings(self.LOCAL)
        settings.olap.terms = "off"
        fusion = build_olap(settings, gateway_factory=None)  # type: ignore[arg-type]
        try:
            assert "answer_question" not in fusion.assistant.tool_names
            assert "query_data" in fusion.assistant.tool_names
        finally:
            fusion.close()

    def test_a_cloud_model_is_shown_everyone_under_the_default_masks(self):
        from truejoin.bootstrap import build_olap

        masking = {"enabled": True, "rules": {"email": "partial"}, "exempt_roles": ["dpo"]}
        for configured, forced in ((masking, True), ({}, False), ({"enabled": True}, False)):
            settings = self.settings(self.CLOUD, share=True, masking=configured)
            fusion = build_olap(settings, gateway_factory=None)  # type: ignore[arg-type]
            try:
                # Nothing to force where no mask is configured for a caller with no roles.
                assert fusion.assistant.mask_everyone is forced
            finally:
                fusion.close()

    def test_with_no_usable_model_the_server_starts_and_the_reason_is_kept(self, monkeypatch):
        from truejoin.bootstrap import assistant_problem, build_olap

        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        keyless = {"default": {"provider": "openai", "model": "gpt"}}
        settings = self.settings(keyless, share=True)
        fusion = build_olap(settings, gateway_factory=None)  # type: ignore[arg-type]
        try:
            assert fusion.assistant is None
            assert "has no model to ask" in fusion.assistant_unavailable
            assert "OPENAI_API_KEY" in fusion.assistant_unavailable
        finally:
            fusion.close()
        assert "no model can be built" in assistant_problem(settings)
        assert assistant_problem(self.settings(self.LOCAL)) == ""
        assert assistant_problem(Settings()) == ""

    def test_a_question_goes_through_the_model_on_the_engines_loop_and_it_is_closed(self):
        from tests.fusion.fakes import ScriptedChatModel, say
        from truejoin.bootstrap import build_olap
        from truejoin.domain.olap.assistant import AnswerEvent

        model = ScriptedChatModel(say("Nothing is connected."))
        fusion = build_olap(
            self.settings(self.LOCAL),
            gateway_factory=None,  # type: ignore[arg-type]
            chat_model_factory=lambda settings, slot: model,
        )
        try:
            (answer,) = fusion.assistant.run("What is there?")
            assert isinstance(answer, AnswerEvent) and answer.text == "Nothing is connected."
            assert model.script.calls[0]["timeout"] <= 120
            assert model.script.sent[0].text.endswith("Tables: none are connected")
        finally:
            fusion.close()
        assert model.closed
