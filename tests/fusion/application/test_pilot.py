"""A pilot on the real engine: questions in words, expected answers as SQL, one caller."""

import pytest

from tests.fusion.application.test_terms import FOREIGN_KEYS, TABLES, catalog, masked, tenant
from tests.fusion.fakes import FakeSourceFactory, ManualScheduler
from truejoin.application.olap.pilot import run_pilot
from truejoin.application.olap.settings import Settings
from truejoin.bootstrap import build_app
from truejoin.domain.olap.pilot import PilotQuestion


@pytest.fixture
def app(tmp_path):
    settings = Settings(threads=1, memory_limit="256MB", backup_path=str(tmp_path / "b"))
    app = build_app(settings, scheduler=ManualScheduler(), source_factory=FakeSourceFactory())
    app.sources.connect("db", {"type": "fake", "tables": TABLES, "foreign_keys": FOREIGN_KEYS})
    app.semantic.use_catalog("db", catalog())
    yield app
    app.close()


PAID = "ödenen sipariş sayısı nedir?"
BY_CITY = "Şehir bazında toplam sipariş tutarı"
CITY_SQL = (
    "SELECT b.city, SUM(o.total) FROM db.orders o JOIN db.customers c ON o.customer_id = c.id "
    "JOIN db.branches b ON c.branch_id = b.id GROUP BY 1"
)


def pilot(app, *questions, scope=None):
    kwargs = {} if scope is None else {"scope": scope}
    return [o.verdict for o in run_pilot(app.tools, app.query, questions, **kwargs)]


def test_each_question_gets_its_verdict(app):
    assert pilot(
        app,
        PilotQuestion(
            "right", PAID, expected_sql="SELECT COUNT(*) FROM db.orders WHERE status = 'paid'"
        ),
        PilotQuestion("wrong", PAID, expected_rows=((3,),)),
        PilotQuestion("refused", "Kargo ücreti ne kadar?", expected_rows=((1,),)),
        PilotQuestion("bad-gold", PAID, expected_sql="SELECT * FROM db.nowhere"),
    ) == ["correct", "wrong", "refused", "unchecked"]


def test_the_expected_answer_runs_as_the_same_caller(app):
    # Tenant "a" sees Izmir only; its expected SQL, run as tenant "a", agrees.
    assert pilot(app, PilotQuestion("c", BY_CITY, expected_sql=CITY_SQL), scope=tenant("a")) == [
        "correct"
    ]
    # Unrestricted, the same expected SQL holds both cities: the answer must too.
    assert pilot(app, PilotQuestion("c", BY_CITY, expected_sql=CITY_SQL)) == ["correct"]


def test_a_masked_caller_is_compared_masked(app):
    question = PilotQuestion(
        "m", "müşteri sayısı", expected_sql="SELECT COUNT(*) FROM db.customers"
    )
    assert pilot(app, question, scope=masked("email")) == ["correct"]
