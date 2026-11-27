"""Tests for the Warp Catalog CLI (click commands).

Uses CliRunner with a temp config file whose catalog.storage_path points at a
tmp dir seeded with a catalog. Store-only commands (list, info, export, review
--auto-approve, enrich-openapi) are exercised end-to-end; analyze/pipeline are
covered only for early/argument paths.
"""

import json
from pathlib import Path

import yaml
from click.testing import CliRunner

from truejoin.adapters.inbound.cli.main import catalog as main  # container factory injected
from truejoin.adapters.outbound.catalog_store.file_store import CatalogFileStore
from truejoin.application.catalog.catalog_review import CatalogReviewService
from truejoin.domain.catalog.catalog import (
    ColumnCatalogEntry,
    DatabaseCatalog,
    LocalizedText,
    TableCatalogEntry,
)


def _seed_catalog(storage_path: Path) -> CatalogFileStore:
    store = CatalogFileStore(storage_path)
    catalog = DatabaseCatalog(
        database_name="testdb",
        database_type="postgresql",
        description=LocalizedText(texts={"en": "A test db"}),
        tables={
            "users": TableCatalogEntry(
                table_name="users",
                description=LocalizedText(texts={"en": "User accounts"}),
                human_name=LocalizedText(texts={"en": "Users"}),
                columns=[
                    ColumnCatalogEntry(name="id", data_type="integer", is_primary_key=True),
                    ColumnCatalogEntry(
                        name="email",
                        data_type="varchar",
                        semantic_type="email",
                        is_foreign_key=False,
                        tags=["pii"],
                        description=LocalizedText(texts={"en": "Email address"}),
                    ),
                ],
                primary_key="id",
                row_count=42,
                tags=["core"],
            ),
        },
        languages=["en"],
        llm_provider="openai",
        llm_model="gpt-4o-mini",
    )
    CatalogReviewService(store).save_as_draft(catalog)
    return store


def _write_config(tmp_path: Path, storage_path: Path) -> Path:
    config = {
        "databases": [
            {
                "name": "testdb",
                "type": "postgresql",
                "host": "localhost",
                "port": 5432,
                "database": "testdb",
                "username": "u",
                "password": "p",
            }
        ],
        "settings": {
            "catalog": {"storage_path": str(storage_path)},
            "audit": {"enabled": True, "file": str(tmp_path / "audit.jsonl")},
        },
    }
    config_file = tmp_path / "database.yaml"
    config_file.write_text(yaml.dump(config))
    return config_file


def _changes(tmp_path: Path) -> list[tuple[str, str, str]]:
    """The catalog changes the audit file holds, as (actor kind, table, change)."""
    path = tmp_path / "audit.jsonl"
    events = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
    assert all(e["event"] == "catalog_change" and e["actor"].startswith("cli") for e in events)
    return [(e["action"], e["table"], e["change"]) for e in events]


def test_version() -> None:
    result = CliRunner().invoke(main, ["--version"])
    assert result.exit_code == 0


def test_help() -> None:
    result = CliRunner().invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "analyze" in result.output


class TestListCommand:
    def test_list_catalogs(self, tmp_path: Path) -> None:
        storage = tmp_path / "catalogs"
        _seed_catalog(storage)
        cfg = _write_config(tmp_path, storage)
        result = CliRunner().invoke(main, ["-c", str(cfg), "list"])
        assert result.exit_code == 0
        assert "testdb" in result.output
        assert "1 tables" in result.output
        assert "status=draft" in result.output

    def test_list_empty(self, tmp_path: Path) -> None:
        storage = tmp_path / "catalogs"
        storage.mkdir()
        cfg = _write_config(tmp_path, storage)
        result = CliRunner().invoke(main, ["-c", str(cfg), "list"])
        assert result.exit_code == 0
        assert "No catalogs found" in result.output


class TestInfoCommand:
    def test_info(self, tmp_path: Path) -> None:
        storage = tmp_path / "catalogs"
        _seed_catalog(storage)
        cfg = _write_config(tmp_path, storage)
        result = CliRunner().invoke(main, ["-c", str(cfg), "info", "-d", "testdb"])
        assert result.exit_code == 0
        assert "Database: testdb" in result.output
        assert "Tables: 1" in result.output
        assert "Users (users)" in result.output
        assert "42 rows" in result.output

    def test_info_not_found(self, tmp_path: Path) -> None:
        storage = tmp_path / "catalogs"
        storage.mkdir()
        cfg = _write_config(tmp_path, storage)
        result = CliRunner().invoke(main, ["-c", str(cfg), "info", "-d", "ghost"])
        assert result.exit_code == 1
        assert "not found" in result.output


class TestExportCommand:
    def test_export_json(self, tmp_path: Path) -> None:
        storage = tmp_path / "catalogs"
        _seed_catalog(storage)
        cfg = _write_config(tmp_path, storage)
        out = tmp_path / "out.json"
        result = CliRunner().invoke(
            main, ["-c", str(cfg), "export", "-d", "testdb", "-o", str(out), "-f", "json"]
        )
        assert result.exit_code == 0
        assert out.exists()
        assert "Exported 1 tables" in result.output

    def test_export_markdown(self, tmp_path: Path) -> None:
        storage = tmp_path / "catalogs"
        _seed_catalog(storage)
        cfg = _write_config(tmp_path, storage)
        out = tmp_path / "out.md"
        result = CliRunner().invoke(
            main,
            ["-c", str(cfg), "export", "-d", "testdb", "-o", str(out), "-f", "markdown"],
        )
        assert result.exit_code == 0
        assert out.exists()
        assert "# testdb" in out.read_text()

    def test_export_not_found(self, tmp_path: Path) -> None:
        storage = tmp_path / "catalogs"
        storage.mkdir()
        cfg = _write_config(tmp_path, storage)
        out = tmp_path / "out.json"
        result = CliRunner().invoke(main, ["-c", str(cfg), "export", "-d", "ghost", "-o", str(out)])
        assert result.exit_code == 1
        assert "not found" in result.output


class TestReviewCommand:
    def test_review_auto_approve(self, tmp_path: Path) -> None:
        storage = tmp_path / "catalogs"
        store = _seed_catalog(storage)
        cfg = _write_config(tmp_path, storage)
        result = CliRunner().invoke(
            main, ["-c", str(cfg), "review", "-d", "testdb", "--auto-approve"]
        )
        assert result.exit_code == 0
        assert "auto-approved" in result.output
        assert store.load_or_raise("testdb").status.value == "approved"
        assert _changes(tmp_path) == [("update", "", "approve_catalog")]

    def test_review_not_found(self, tmp_path: Path) -> None:
        storage = tmp_path / "catalogs"
        storage.mkdir()
        cfg = _write_config(tmp_path, storage)
        result = CliRunner().invoke(main, ["-c", str(cfg), "review", "-d", "ghost"])
        assert result.exit_code == 1
        assert "not found" in result.output

    def test_review_interactive_approve_all(self, tmp_path: Path) -> None:
        storage = tmp_path / "catalogs"
        store = _seed_catalog(storage)
        cfg = _write_config(tmp_path, storage)
        # single table -> answer "a" to approve it, then it auto-approves catalog
        result = CliRunner().invoke(main, ["-c", str(cfg), "review", "-d", "testdb"], input="a\n")
        assert result.exit_code == 0
        assert store.load_or_raise("testdb").status.value == "approved"

    def test_review_interactive_quit(self, tmp_path: Path) -> None:
        storage = tmp_path / "catalogs"
        store = _seed_catalog(storage)
        cfg = _write_config(tmp_path, storage)
        result = CliRunner().invoke(main, ["-c", str(cfg), "review", "-d", "testdb"], input="q\n")
        assert result.exit_code == 0
        assert "paused" in result.output
        assert store.load_or_raise("testdb").status.value == "draft"
        assert _changes(tmp_path) == []

    def test_review_edits_are_audited_by_name(self, tmp_path: Path) -> None:
        storage = tmp_path / "catalogs"
        _seed_catalog(storage)
        cfg = _write_config(tmp_path, storage)
        result = CliRunner().invoke(
            main,
            ["-c", str(cfg), "review", "-d", "testdb"],
            input="e\n1\nSecret words\n5\nemail\nnational_id\n7\nq\n",
        )
        assert result.exit_code == 0, result.output
        assert _changes(tmp_path) == [
            ("update", "users", "edit_table description"),
            ("update", "users", "edit_column email semantic_type"),
        ]
        assert "Secret words" not in (tmp_path / "audit.jsonl").read_text()

    def test_review_already_approved_decline_reopen(self, tmp_path: Path) -> None:
        storage = tmp_path / "catalogs"
        store = _seed_catalog(storage)
        CatalogReviewService(store).approve_catalog("testdb")
        cfg = _write_config(tmp_path, storage)
        result = CliRunner().invoke(main, ["-c", str(cfg), "review", "-d", "testdb"], input="n\n")
        assert result.exit_code == 0
        assert "already approved" in result.output


class TestEnrichOpenapiCommand:
    def test_enrich_openapi(self, tmp_path: Path) -> None:
        storage = tmp_path / "catalogs"
        _seed_catalog(storage)
        cfg = _write_config(tmp_path, storage)
        spec = tmp_path / "spec.json"
        spec.write_text('{"openapi": "3.0.0", "info": {"title": "x", "version": "1"}, "paths": {}}')
        out = tmp_path / "enriched.json"
        result = CliRunner().invoke(
            main,
            [
                "-c",
                str(cfg),
                "enrich-openapi",
                "-d",
                "testdb",
                "-i",
                str(spec),
                "-o",
                str(out),
            ],
        )
        assert result.exit_code == 0
        assert out.exists()

    def test_enrich_openapi_catalog_not_found(self, tmp_path: Path) -> None:
        storage = tmp_path / "catalogs"
        storage.mkdir()
        cfg = _write_config(tmp_path, storage)
        spec = tmp_path / "spec.json"
        spec.write_text('{"openapi": "3.0.0", "paths": {}}')
        result = CliRunner().invoke(
            main, ["-c", str(cfg), "enrich-openapi", "-d", "ghost", "-i", str(spec)]
        )
        assert result.exit_code == 1
        assert "not found" in result.output


class TestAnalyzeEarlyPath:
    def test_analyze_db_config_not_found(self, tmp_path: Path) -> None:
        storage = tmp_path / "catalogs"
        storage.mkdir()
        cfg = _write_config(tmp_path, storage)
        result = CliRunner().invoke(main, ["-c", str(cfg), "analyze", "-d", "missing_db"])
        assert result.exit_code == 1
        assert "not found" in result.output


def _seed_term(store: CatalogFileStore) -> None:
    from truejoin.domain.catalog.terms import ColumnAttribute, SqlAttribute, Term

    catalog = store.load_or_raise("testdb")
    catalog.terms = {
        "User": Term(
            name="User",
            synonyms=["member"],
            tables=["users"],
            column_attributes=[
                ColumnAttribute(
                    table="users", column="email", synonyms=["mail"], values={"a@b.c": ["ann"]}
                )
            ],
            sql_attributes=[SqlAttribute(name="has_mail", table="users", expression="1")],
        )
    }
    store.save(catalog)


class TestTerms:
    def _setup(self, tmp_path: Path):
        storage = tmp_path / "catalogs"
        store = _seed_catalog(storage)
        _seed_term(store)
        return store, _write_config(tmp_path, storage)

    def test_review_shows_the_term_and_approving_the_table_certifies_it(self, tmp_path: Path):
        store, cfg = self._setup(tmp_path)
        result = CliRunner().invoke(main, ["-c", str(cfg), "review", "-d", "testdb"], input="a\n")
        assert result.exit_code == 0, result.output
        assert "Term: User [not certified]" in result.output
        assert "Also called: member" in result.output
        assert "email: mail  [a@b.c = ann]" in result.output
        assert "Expression has_mail [not certified]: 1" in result.output
        term = store.load_or_raise("testdb").terms["User"]
        assert term.certified and not term.sql_attributes[0].certified

    def test_the_reviewer_can_edit_the_term_before_approving(self, tmp_path: Path):
        store, cfg = self._setup(tmp_path)
        keys = ["e", "9", "üye, member", "10", "email", "e-posta", "11", "has_mail", "8", "Member"]
        result = CliRunner().invoke(
            main,
            ["-c", str(cfg), "review", "-d", "testdb"],
            input="\n".join([*keys, "7", "a"]) + "\n",
        )
        assert result.exit_code == 0, result.output
        term = store.load_or_raise("testdb").terms["Member"]
        assert term.synonyms == ["üye", "member"]
        assert term.attribute("users", "email").synonyms == ["e-posta"]
        assert term.sql_attributes[0].certified and term.certified
        # Names only: never the words the reviewer typed.
        assert _changes(tmp_path) == [
            ("update", "users", "edit_term User synonyms"),
            ("update", "users", "edit_term User columns"),
            ("update", "users", "edit_term User certify_sql certify_sql=has_mail"),
            ("update", "users", "edit_term User name"),
            ("update", "users", "approve_table certify_term"),
            ("update", "", "approve_catalog"),
        ]
        assert "e-posta" not in (tmp_path / "audit.jsonl").read_text()

    def test_an_edit_that_names_nothing_is_said_not_raised(self, tmp_path: Path):
        _, cfg = self._setup(tmp_path)
        result = CliRunner().invoke(
            main,
            ["-c", str(cfg), "review", "-d", "testdb"],
            input="e\n10\nnope\nx\n7\nq\n",
        )
        assert result.exit_code == 0 and "Term 'User' has no column 'nope'" in result.output

    def test_approve_leaves_terms_alone_and_says_so(self, tmp_path: Path):
        store, cfg = self._setup(tmp_path)
        result = CliRunner().invoke(main, ["-c", str(cfg), "approve", "-d", "testdb"])
        assert result.exit_code == 0
        assert "1 term(s) are not certified and will not be used" in result.output
        assert store.load_or_raise("testdb").certified_terms == []

    def test_approve_certifies_them_when_told_to(self, tmp_path: Path):
        store, cfg = self._setup(tmp_path)
        result = CliRunner().invoke(
            main, ["-c", str(cfg), "approve", "-d", "testdb", "--certify-terms"]
        )
        assert result.exit_code == 0 and "1 term(s) certified" in result.output
        assert [t.name for t in store.load_or_raise("testdb").certified_terms] == ["User"]
        assert _changes(tmp_path) == [
            ("update", "", "approve_catalog"),
            ("update", "", "certify_terms 1"),
        ]

    def test_a_table_without_a_term_says_why(self, tmp_path: Path):
        storage = tmp_path / "catalogs"
        _seed_catalog(storage)
        cfg = _write_config(tmp_path, storage)
        result = CliRunner().invoke(
            main, ["-c", str(cfg), "review", "-d", "testdb"], input="e\n9\n7\nq\n"
        )
        assert "This table has no term" in result.output


class TestSuggestAndCertify:
    """`catalog suggest` writes drafts; `catalog certify` is what makes one a measure."""

    def _setup(self, tmp_path: Path, statements, views=()):
        from truejoin.adapters.outbound.sqlglot_expressions import SqlglotExpressionMiner
        from truejoin.application.catalog.history_mining import HistoryMiningService
        from truejoin.bootstrap import build_container, build_repository, load_settings
        from truejoin.domain.catalog.history import HistoryEntry, HistoryUnavailableError

        storage = tmp_path / "catalogs"
        store = _seed_catalog(storage)
        _seed_term(store)
        cfg = _write_config(tmp_path, storage)

        class History:
            async def recent_statements(self, limit: int = 500):
                if isinstance(statements, str):
                    raise HistoryUnavailableError(statements)
                return [HistoryEntry(sql, calls) for sql, calls in statements]

            async def view_definitions(self):
                if isinstance(views, str):
                    raise HistoryUnavailableError(views)
                return [HistoryEntry(sql, origin="view", name=name) for name, sql in views]

        class Gateway:
            async def connect(self):
                return None

            async def disconnect(self):
                return None

        class Gateways:
            def create(self, config):
                return Gateway()

        class Model:
            async def generate_json(self, prompt, system_prompt=None):
                return (
                    '{"keep": [{"id": 1, "name": "work_mail", "description": "A work address."}]}'
                )

            async def close(self):
                return None

        def factory(config_path: str):
            settings = load_settings(config_path)
            return build_container(
                settings,
                gateway_factory=Gateways(),
                text_generator_factory=Model,
                mining_factory=lambda gateway, db, model: HistoryMiningService(
                    history=History(),
                    miner=SqlglotExpressionMiner(),
                    text_generator=model,
                    review=CatalogReviewService(build_repository(settings)),
                    db_type="postgresql",
                    database_name=db.name,
                ),
            )

        def run(*args: str):
            return CliRunner().invoke(
                main, ["-c", str(cfg), *args], obj={"container_factory": factory}
            )

        return store, run

    WORK = "SELECT COUNT(*) FROM users WHERE email LIKE '%@corp.example'"
    FLAG = "CASE WHEN email LIKE '%@corp.example' THEN 1 ELSE 0 END"

    def test_suggest_stores_a_draft_and_says_how_to_certify_it(self, tmp_path: Path):
        store, run = self._setup(tmp_path, [(self.WORK, 12)])
        result = run("suggest", "-d", "testdb")
        assert result.exit_code == 0, result.output
        assert "Read 1 statement(s) and 0 view(s)." in result.output
        assert f"users.work_mail = {self.FLAG}" in result.output
        assert "A work address." in result.output
        assert "catalog certify -d testdb -e work_mail" in result.output
        drafts = store.load_or_raise("testdb").terms["User"].sql_attributes
        assert [(a.name, a.certified) for a in drafts] == [
            ("has_mail", False),
            ("work_mail", False),
        ]

    def test_certify_turns_the_draft_into_a_certified_expression(self, tmp_path: Path):
        store, run = self._setup(tmp_path, [(self.WORK, 12)])
        run("suggest", "-d", "testdb")
        result = run("certify", "-d", "testdb", "-e", "work_mail")
        assert result.exit_code == 0 and f"Certified users.work_mail = {self.FLAG}" in result.output
        by_name = {a.name: a for a in store.load_or_raise("testdb").terms["User"].sql_attributes}
        assert by_name["work_mail"].certified and not by_name["has_mail"].certified
        assert _changes(tmp_path) == [
            ("update", "users", "suggest_sql work_mail"),
            ("update", "users", "certify_sql=work_mail"),
        ]

    def test_certify_refuses_a_name_that_is_not_there(self, tmp_path: Path):
        _, run = self._setup(tmp_path, [])
        result = run("certify", "-d", "testdb", "-e", "nope")
        assert result.exit_code == 1 and "uncertified: has_mail" in result.output

    def test_a_history_that_cannot_be_read_is_said_and_the_views_still_count(self, tmp_path: Path):
        views = [("corp_users", "SELECT id FROM users WHERE email LIKE '%@corp.example'")]
        _, run = self._setup(tmp_path, "It needs GRANT SELECT ON V_$SQL.", views)
        result = run("suggest", "-d", "testdb")
        assert result.exit_code == 0, result.output
        assert "Not read: It needs GRANT SELECT ON V_$SQL." in result.output
        assert (
            "Read 0 statement(s) and 1 view(s)." in result.output and "work_mail" in result.output
        )

    def test_with_neither_source_readable_it_fails_and_says_why(self, tmp_path: Path):
        _, run = self._setup(tmp_path, "no history here", "no views here")
        result = run("suggest", "-d", "testdb")
        assert result.exit_code == 1
        assert "Not read: no history here" in result.output and "Nothing new" in result.output

    def test_nothing_worth_suggesting_is_not_an_error(self, tmp_path: Path):
        _, run = self._setup(tmp_path, [("SELECT * FROM users WHERE id = 3", 99)])
        result = run("suggest", "-d", "testdb")
        assert result.exit_code == 0 and "Nothing new to suggest." in result.output

    def test_suggest_without_a_catalog_says_to_analyze_first(self, tmp_path: Path):
        _, run = self._setup(tmp_path, [])
        result = run("suggest", "-d", "ghost")
        assert result.exit_code == 1 and "Run `catalog analyze` first" in result.output


class TestImportCommand:
    def test_an_ossie_model_is_merged_as_drafts_and_says_what_it_skipped(
        self, tmp_path: Path
    ) -> None:
        storage = tmp_path / "catalogs"
        _seed_catalog(storage)
        cfg = _write_config(tmp_path, storage)
        model = tmp_path / "model.yaml"
        model.write_text(
            "version: 0.2.0.dev0\n"
            "name: shop\n"
            "datasets:\n"
            "  - name: users\n"
            "    source: users\n"
            "    metrics:\n"
            "      - {name: people, expression: {sql: 'COUNT(DISTINCT id)'}}\n"
            "      - {name: rate, expression: {sql: 'SUM(id) / COUNT(id)'}}\n"
        )
        result = CliRunner().invoke(
            main, ["-c", str(cfg), "import", "-d", "testdb", "-f", "ossie", str(model)]
        )
        assert result.exit_code == 0, result.output
        assert "1 measure(s), as drafts" in result.output
        assert "skipped metric users.rate" in result.output

    def test_a_catalog_there_is_needs_replace(self, tmp_path: Path) -> None:
        storage = tmp_path / "catalogs"
        _seed_catalog(storage)
        cfg = _write_config(tmp_path, storage)
        from truejoin.adapters.outbound.catalog_store.file_store import CatalogFileStore

        exported = tmp_path / "whole.json"
        exported.write_text(CatalogFileStore(storage).load("testdb").model_dump_json())
        refused = CliRunner().invoke(
            main, ["-c", str(cfg), "import", "-d", "testdb", str(exported)]
        )
        assert refused.exit_code == 1 and "--replace" in refused.output
        replaced = CliRunner().invoke(
            main, ["-c", str(cfg), "import", "-d", "testdb", "--replace", str(exported)]
        )
        assert replaced.exit_code == 0, replaced.output
