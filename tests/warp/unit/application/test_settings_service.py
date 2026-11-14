"""The console's settings over the file's: what may change, and how a change is checked."""

from contextlib import nullcontext

import pytest
from cryptography.fernet import Fernet

from truejoin.adapters.outbound.settings_store.encrypted_json import EncryptedJsonSettingsStore
from truejoin.application.settings import Settings
from truejoin.application.settings_service import SettingsError, SettingsService, model_paths
from truejoin.domain.settings_catalog import (
    BOOTSTRAP,
    DANGEROUS,
    HELP,
    HIDDEN,
    is_bootstrap,
    is_dangerous,
    is_secret,
    section_of,
    split_label,
)
from truejoin.ports.settings_store import StoredSetting, StoredSettings


class MemoryStore:
    def __init__(self):
        self.kept = StoredSettings()

    def version(self):
        return id(self.kept)

    def load(self):
        return self.kept

    def save(self, settings):
        self.kept = settings

    def locked(self):
        return nullcontext()


def service(file_paths=frozenset()):
    file = Settings(settings={"pagination": {"default_limit": 20}}).with_file_paths(file_paths)
    return SettingsService(MemoryStore(), file)


class TestCatalog:
    def test_every_rule_names_a_real_setting(self):
        paths = set(model_paths(Settings))
        for rule in (*BOOTSTRAP, *HIDDEN, *DANGEROUS):
            assert any(p == rule or (rule.endswith(".") and p.startswith(rule)) for p in paths), (
                rule
            )

    def test_what_locks_people_out_is_bootstrap(self):
        for path in ("settings.auth.enabled", "settings.auth.api_keys", "settings.audit.enabled"):
            assert is_bootstrap(path)
        assert not is_bootstrap("settings.audit.question_text")
        assert is_dangerous("mcp.tools") and not is_dangerous("settings.pagination.max_limit")
        assert is_secret("settings.llm.default.api_key") and not is_dangerous(
            "settings.llm.default.api_key"
        )

    def test_the_languages_an_analysis_writes_in_are_set_from_the_console(self):
        # They were hidden as unread; the analysis reads both.
        rows = {r["path"]: r for r in service().effective(Settings())}
        languages = rows["settings.i18n.languages"]
        assert (languages["kind"], languages["section"], languages["locked"]) == (
            "list",
            "Language",
            "",
        )
        assert "analyze a catalog again" in languages["help"]
        assert rows["settings.i18n.translation_strategy"]["options"] == ["single", "multi"]
        assert "settings.i18n.auto_translate" not in rows

    def test_sections_and_labels(self):
        assert section_of("olap.remote.url") == "Remote sources"
        assert section_of("olap.cache_ttl") == "Analytics engine"
        assert section_of("settings.excluded_tables") == "API"
        assert section_of("settings.auth.enabled") == "Startup (file only)"
        assert split_label("settings.llm.reasoning.model") == ("Reasoning", "Model")
        assert split_label("settings.pagination.max_limit") == ("", "Max limit")

    def test_every_setting_the_console_changes_says_what_it_does(self):
        rows = service().effective(Settings())
        silent = [r["path"] for r in rows if not r["locked"] and not r["help"]]
        assert silent == []
        assert set(HELP) <= {r["path"] for r in rows}


class TestChanges:
    def test_a_change_validates_and_is_kept_on_commit(self):
        settings_service = service()
        merged, stored = settings_service.prepare(
            {"settings.pagination.max_limit": 5}, (), by="admin", app_env="development"
        )
        assert merged.settings.pagination.max_limit == 5
        assert settings_service.merged().settings.pagination.max_limit == 1000  # not yet kept
        settings_service.commit(stored)
        assert settings_service.merged().settings.pagination.max_limit == 5

    @pytest.mark.parametrize(
        ("changes", "confirm", "message"),
        [
            ({"settings.auth.enabled": False}, (), "configuration file only"),
            ({"settings.pagination.default_limit": 9}, (), "locks it"),
            ({"settings.nope": 1}, (), "not a setting"),
            ({"mcp.tools": ["list_sources"]}, (), "confirm"),
            ({"olap.rate_limit": "lots"}, (), "rate_limit"),
            ({"mcp.tools": ["no_such_tool"]}, ("mcp.tools",), "unknown tools"),
        ],
    )
    def test_what_is_refused(self, changes, confirm, message):
        with pytest.raises(SettingsError, match=message):
            service({"settings.pagination.default_limit"}).prepare(
                changes, confirm, by=None, app_env="development"
            )

    def test_production_rules_hold(self):
        with pytest.raises(SettingsError, match="enable_raw_query"):
            service().prepare(
                {"settings.enable_raw_query": True},
                ("settings.enable_raw_query",),
                by=None,
                app_env="production",
            )

    def test_reset_and_a_blank_secret(self):
        settings_service = service()
        path = "settings.llm.default.api_key"
        _, stored = settings_service.prepare({path: "sk-1"}, (), by=None, app_env="development")
        settings_service.commit(stored)
        _, stored = settings_service.prepare({path: ""}, (), by=None, app_env="development")
        assert stored.values[path].value == "sk-1"
        _, stored = settings_service.prepare({}, (), by=None, app_env="development", reset=[path])
        assert path not in stored.values

    def test_the_file_wins_over_a_stored_value(self):
        settings_service = service({"settings.pagination.default_limit"})
        settings_service.commit(
            StoredSettings({"settings.pagination.default_limit": StoredSetting(99)})
        )
        assert settings_service.merged().settings.pagination.default_limit == 20

    def test_effective_shows_sources_and_hides_secrets(self):
        settings_service = service({"settings.pagination.default_limit"})
        rows = {r["path"]: r for r in settings_service.effective(settings_service.merged())}
        assert rows["settings.pagination.default_limit"]["source"] == "file"
        assert rows["settings.pagination.max_limit"]["source"] == "default"
        assert rows["settings.auth.enabled"]["locked"] == "bootstrap"
        assert rows["settings.llm.default.api_key"]["value"] is None
        assert (
            rows["olap.grounding"]["kind"] == "choice"
            and "repair" in rows["olap.grounding"]["options"]
        )
        assert "settings.docs_url" not in rows


def test_the_store_encrypts_secrets(tmp_path):
    path = tmp_path / "settings.json"
    key = Fernet.generate_key()
    store = EncryptedJsonSettingsStore(path, key)
    store.save(
        StoredSettings(
            {
                "settings.llm.default.api_key": StoredSetting("sk-secret", "admin"),
                "olap.cache_ttl": StoredSetting(60),
            }
        )
    )
    text = path.read_text()
    assert "sk-secret" not in text and '"value": 60' in text
    loaded = EncryptedJsonSettingsStore(path, key).load().values
    assert loaded["settings.llm.default.api_key"].value == "sk-secret"
    assert loaded["olap.cache_ttl"].changed_by is None
