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
        for name in ("database", "memory_limit", "threads", "cache_ttl", "rate_limit"):
            assert getattr(mapped, name) == getattr(plain, name)
        assert mapped.policy() == plain.policy()
        assert mapped.warp_http_defaults() == plain.warp_http_defaults()

    def test_every_field_reaches_the_engine_settings(self):
        olap = OlapConfig(
            database="/data/olap.duckdb",
            threads=2,
            slice_max_rows=7,
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
        untouched = {"enabled", "grounding", "terms", "temp_directory", "unread_questions"}
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
    def test_refused_unless_asked_and_then_a_model_writes_the_sql(self):
        from truejoin.bootstrap import build_olap

        assert OlapConfig().unread_questions == "refuse"
        local = LLMTriplet(default={"provider": "ollama", "model": "qwen2.5-coder:7b"})
        cloud = LLMTriplet(default={"provider": "openai", "model": "gpt", "api_key": "sk-test"})
        cases = (
            ("refuse", local, False, True),
            ("sql", local, True, True),
            ("sql", cloud, True, False),
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
