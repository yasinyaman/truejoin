"""The seventh arm's parts, with no model in the loop."""

import json

import duckdb
import pytest

from bench import terms
from bench.arms import AnswerArm, GapsOnlyArm, ScriptedModel, _named_in
from bench.constrain import request_schema
from bench.dataset import load_question_set
from bench.fixture import (
    ISLEMLER,
    ISLEMLER_COLUMNS,
    MUSTERILER,
    MUSTERILER_COLUMNS,
    SUBELER,
    SUBELER_COLUMNS,
    as_records,
    load_duckdb,
)
from bench.models import GatewayExecutor, OllamaModel
from bench.run_duckdb import term_arms
from bench.runner import run_arm
from trio.application.catalog.term_extraction import build_term

TABLES = {
    "subeler": as_records(SUBELER, SUBELER_COLUMNS),
    "musteriler": as_records(MUSTERILER, MUSTERILER_COLUMNS),
    "islemler": as_records(ISLEMLER, ISLEMLER_COLUMNS),
}
NAMES = ["banka.islemler", "banka.musteriler", "banka.subeler"]


@pytest.fixture
def catalog_path(tmp_path):
    """A catalog whose terms carry no words at all: only the names and stored values."""
    catalog = terms.fixture_catalog()
    catalog.terms = {t.table_name: build_term(t, None, "en") for t in catalog.tables.values()}
    path = tmp_path / "banka.json"
    path.write_text(catalog.model_dump_json(), "utf-8")
    return path


@pytest.fixture
def questions():
    return load_question_set("bench/questions/banka-tr-en.json")


@pytest.fixture
def gold(questions):
    connection = load_duckdb(duckdb.connect())
    return {
        q.id: [tuple(r) for r in connection.execute(q.gold_for("duckdb")).fetchall()]
        for q in questions
    }


class TestFixtureCatalog:
    def test_it_is_the_fixture_as_analysis_would_have_kept_it(self):
        catalog = terms.fixture_catalog()
        assert list(catalog.tables) == ["subeler", "musteriler", "islemler"]
        islemler = catalog.tables["islemler"]
        assert islemler.get_column("musteri_id").references == "musteriler.musteri_id"
        assert islemler.get_column("durum").sample_values == ["basarili", "iptal"]
        assert len(islemler.get_column("tutar").sample_values) == terms.SAMPLES

    def test_no_sample_is_kept_of_a_personal_column(self):
        musteriler = terms.fixture_catalog().tables["musteriler"]
        assert musteriler.get_column("eposta").sample_values == []
        assert musteriler.get_column("ad_soyad").sample_values == []

    def test_loading_signs_the_review(self, catalog_path):
        catalog = terms.load_certified(catalog_path)
        assert len(catalog.certified_terms) == 3

    def test_a_model_that_wrote_nothing_is_an_error_not_a_vocabulary(self, tmp_path, monkeypatch):
        class Silent:
            def __init__(self, model: str) -> None:
                pass

            async def generate_json(self, prompt, system_prompt=None):
                raise RuntimeError("connection refused")

        monkeypatch.setattr(terms, "OllamaText", Silent)
        with pytest.raises(SystemExit, match="wrote no words for"):
            terms.generate("any", tmp_path / "out.json")
        assert not (tmp_path / "out.json").exists()


class TestTermArms:
    def test_there_are_none_until_a_catalog_was_generated(self, tmp_path):
        assert term_arms(ScriptedModel([]), TABLES, NAMES, tmp_path / "missing.json") == []

    def test_the_tool_arm_answers_a_question_that_needs_a_table_in_between(
        self, catalog_path, questions, gold
    ):
        answer_arm, *_ = term_arms(ScriptedModel([]), TABLES, NAMES, catalog_path)
        assert isinstance(answer_arm, AnswerArm) and answer_arm.name == "terms"
        picked = [q for q in questions if q.id in ("q21-tr", "q23-tr", "q05-tr")]
        result = run_arm(answer_arm, picked, gold, "duckdb", questions.identifiers)
        assert {qid: g.correct for qid, g in result.grades.items()} == {
            "q21-tr": True,
            "q23-tr": True,
            "q05-tr": True,
        }
        assert result.hallucination_rate(list(result.grades)) == 0.0

    def test_the_gated_arm_lets_the_model_correct_the_proposal_under_the_schema(
        self, catalog_path, questions, gold
    ):
        """q20 asks for two breakdowns; word matching finds one, the model adds the other."""
        fixed = {
            "table": "banka.islemler",
            "metrics": [{"measure": "*", "aggregation": "count"}],
            "dimensions": [{"column": "musteriler.segment"}, {"column": "kanal"}],
        }
        model = ScriptedModel([json.dumps(fixed)])
        *_, arm = term_arms(model, TABLES, NAMES, catalog_path)
        assert arm.name == "catalog+join-path"
        question = next(q for q in questions if q.id == "q20-tr")
        result = run_arm(arm, [question], gold, "duckdb", questions.identifiers)
        assert result.grades["q20-tr"].correct and not result.grades["q20-tr"].hallucinated
        # The proposal it was shown, and the constraint it answered under.
        assert '"musteriler.segment"' in model.prompts[0] or "kanal" in model.prompts[0]
        assert "musteriler.segment (premium | standart | temel)" in model.prompts[0]
        assert model.schemas[0] is not None

    def test_only_a_question_with_a_word_unread_reaches_the_model(
        self, catalog_path, questions, gold
    ):
        """q21-tr is read word for word (the tool arm answers it); q20-en has words left over."""
        fixed = {
            "table": "banka.islemler",
            "metrics": [{"measure": "*", "aggregation": "count"}],
            "dimensions": [{"column": "musteriler.segment"}, {"column": "kanal"}],
        }
        model = ScriptedModel([json.dumps(fixed)])
        _, arm, _ = term_arms(model, TABLES, NAMES, catalog_path)
        assert isinstance(arm, GapsOnlyArm) and arm.name == "terms+gaps"
        picked = [q for q in questions if q.id in ("q21-tr", "q20-en")]
        covered = {
            q.id: arm._fusion.tools.execute("check_question", {"question": q.text})["covered"]
            for q in picked
        }
        result = run_arm(arm, picked, gold, "duckdb", questions.identifiers)
        assert covered["q20-en"] != "all"  # so the model path is tested too
        assert arm.asked_model == [q for q, c in covered.items() if c != "all"]
        assert len(model.prompts) == len(arm.asked_model)
        assert covered["q21-tr"] == "all" and result.grades["q21-tr"].correct
        assert result.grades["q20-en"].correct  # the model's reply, not the proposal

    def test_the_schema_offers_a_referenced_tables_columns_and_no_others(self, catalog_path):
        *_, arm = term_arms(ScriptedModel([]), TABLES, NAMES, catalog_path)
        by_table = {
            b["properties"]["table"]["const"]: b["properties"] for b in arm._schema["oneOf"]
        }

        def dimensions(table: str) -> set[str]:
            options = by_table[table]["dimensions"]["items"]["oneOf"]
            return {o["properties"]["column"]["const"] for o in options}

        assert {"musteriler.segment", "subeler.sehir", "kanal"} <= dimensions("banka.islemler")
        # The key both tables hold is the table's own; a child is never offered.
        assert "musteriler.musteri_id" not in dimensions("banka.islemler")
        assert not any("." in name for name in dimensions("banka.subeler"))
        assert not any("islemler." in name for name in dimensions("banka.musteriler"))
        cities = next(
            o["properties"]["value"]["enum"]
            for o in by_table["banka.islemler"]["filters"]["items"]["oneOf"]
            if o["properties"]["column"]["const"] == "subeler.sehir"
        )
        assert cities == ["Istanbul", "Ankara", "Izmir"]
        assert "subeler_sehir" in by_table["banka.islemler"]["order_by"]["enum"]


class TestSchemaWithoutParents:
    def test_nothing_changes_for_the_arms_that_do_not_join(self, catalog_path):
        *_, arm = term_arms(ScriptedModel([]), TABLES, NAMES, catalog_path)
        engine = arm._fusion
        plain = request_schema(engine.semantic, ["banka.islemler"])
        options = plain["properties"]["dimensions"]["items"]["oneOf"]
        assert not any("." in o["properties"]["column"]["const"] for o in options)


def test_a_dotted_name_is_a_table_and_a_column_not_one_invented_thing():
    request = {
        "metrics": ["tutar:sum"],
        "dimensions": ["subeler.sehir", "islem_tarihi:month"],
        "filters": [{"column": "musteriler.segment", "value": "premium"}],
    }
    assert _named_in(request, "banka.islemler") == (
        "islem_tarihi",
        "islemler",
        "musteriler",
        "segment",
        "sehir",
        "subeler",
        "tutar",
    )


class TestGatewayExecutor:
    class Gateway:
        def __init__(self) -> None:
            self.connected = False
            self.statements: list[str] = []

        async def connect(self) -> None:
            self.connected = True

        async def disconnect(self) -> None:
            self.connected = False

        async def execute_query(self, query: str, params=None):
            self.statements.append(query)
            return [{"sehir": "Izmir", "n": 2}, {"sehir": "Ankara", "n": 1}]

    def test_rows_come_back_as_tuples_in_column_order(self):
        gateway = self.Gateway()
        executor = GatewayExecutor(gateway)
        try:
            assert gateway.connected
            assert executor.run("SELECT sehir, COUNT(*) n FROM t") == [("Izmir", 2), ("Ankara", 1)]
            assert gateway.statements == ["SELECT sehir, COUNT(*) n FROM t"]
        finally:
            executor.close()
        assert not gateway.connected


class TestModelHost:
    def test_the_host_comes_from_the_environment_like_ollamas_own_tools(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_HOST", "gb10:11434")
        assert OllamaModel()._url == "http://gb10:11434/api/generate"
        assert OllamaModel(host="http://other:1")._url == "http://other:1/api/generate"
        monkeypatch.delenv("OLLAMA_HOST")
        assert OllamaModel()._url == "http://localhost:11434/api/generate"


class TestTheWholeRun:
    def test_the_run_includes_the_term_arms_and_reports_the_gate(self, tmp_path, monkeypatch):
        """The arms were once built, tested and left out of the run."""
        from bench import run_duckdb

        class Unreachable:
            def __init__(self, **_) -> None:
                pass

            def complete(self, prompt, schema=None):
                raise RuntimeError("no model in a unit test")

        monkeypatch.setattr(run_duckdb, "OllamaModel", Unreachable)
        assert run_duckdb.main("none", str(tmp_path)) == 0
        report = json.loads((tmp_path / "banka-tr-en-duckdb.json").read_text())
        assert [arm["name"] for arm in report["arms"]] == [
            "raw",
            "catalog",
            "semantic",
            "lexicon",
            "semantic-schema",
            "lexicon+repair",
            "terms",
            "terms+gaps",
            "catalog+join-path",
        ]
        assert report["questions"] == 48
        # With no model the gated arm answers nothing, and the gate says so.
        assert report["term_gate"].startswith("FAIL: catalog+join-path")
        terms_arm = next(arm for arm in report["arms"] if arm["name"] == "terms")
        assert terms_arm["hallucination_rate"] == 0.0 and terms_arm["accuracy"]["point"] > 0
