"""Many-to-many through a link table: each row counted once per group it falls in.

A student takes several courses through ``enrollments``, which holds nothing
but the two keys. Asked from the students' side, a breakdown by course or
department, or a condition on them, reaches across it. Every answer here is
checked against SQL written by hand with the DISTINCT a careful person adds.
"""

import pytest

from tests.fusion.fakes import FakeSourceFactory, ManualScheduler
from truejoin.application.olap.settings import Settings
from truejoin.bootstrap import build_app
from truejoin.domain.governance.access import AccessScope
from truejoin.domain.governance.row_policy import build_policy
from truejoin.domain.olap.models import TableRef

TABLES = {
    "departments": [{"id": 1, "name": "CS"}, {"id": 2, "name": "Math"}],
    "courses": [
        {"id": 1, "department_id": 1, "title": "Algo"},
        {"id": 2, "department_id": 1, "title": "DB"},
        {"id": 3, "department_id": 2, "title": "Calc"},
    ],
    "students": [
        {"id": 1, "name": "Ann", "tenant": "a", "balance": 10.0},
        {"id": 2, "name": "Bo", "tenant": "b", "balance": 20.0},
        {"id": 3, "name": "Cy", "tenant": "a", "balance": 5.0},
        {"id": 4, "name": "Di", "tenant": "a", "balance": 7.0},  # takes nothing
    ],
    # The link: keys only. Ann's Algo is in twice, as a careless load leaves it.
    "enrollments": [
        {"student_id": 1, "course_id": 1},
        {"student_id": 1, "course_id": 2},
        {"student_id": 2, "course_id": 1},
        {"student_id": 3, "course_id": 3},
        {"student_id": 1, "course_id": 1},
    ],
    # Also between students and courses, but a fact of its own (a score): not a link.
    "grades": [{"id": 1, "student_id": 1, "course_id": 3, "score": 90}],
    # A second link from students, for the one-crossing rule.
    "clubs": [{"id": 1, "name": "Chess"}],
    "memberships": [{"id": 1, "student_id": 1, "club_id": 1}],
}
FOREIGN_KEYS = {
    "courses": [("department_id", "departments", "id")],
    "enrollments": [("student_id", "students", "id"), ("course_id", "courses", "id")],
    "grades": [("student_id", "students", "id"), ("course_id", "courses", "id")],
    "memberships": [("student_id", "students", "id"), ("club_id", "clubs", "id")],
}

ACROSS = (
    "FROM db.students s JOIN (SELECT DISTINCT e.student_id, {cols} FROM db.enrollments e "
    "JOIN db.courses c ON e.course_id = c.id "
    "JOIN db.departments d ON c.department_id = d.id {where}) x ON s.id = x.student_id"
)


@pytest.fixture
def app(tmp_path):
    settings = Settings(threads=1, memory_limit="256MB", backup_path=str(tmp_path / "b"))
    engine = build_app(settings, scheduler=ManualScheduler(), source_factory=FakeSourceFactory())
    engine.sources.connect("db", {"type": "fake", "tables": TABLES, "foreign_keys": FOREIGN_KEYS})
    yield engine
    engine.close()


def rows(result: dict) -> list[tuple]:
    assert "error" not in result, result
    return [tuple(row[c] for c in result["columns"]) for row in result["rows"]]


def sql(app, query: str) -> list[tuple]:
    return [tuple(r) for r in app.query.sql(query).rows]


def metrics(app, table: str, *names: str, scope=None, **kwargs) -> dict:
    tools = app.tools.for_scope(scope) if scope else app.tools
    return tools.execute("query_metrics", {"table": table, "metrics": list(names), **kwargs})


def test_only_a_table_of_keys_is_a_link(app):
    across = app.catalog.joins.across(TableRef("db", "students"))
    assert {ref.table for ref in across} == {"courses", "departments", "clubs"}
    # grades holds a score: asked about as itself, never a way between two others.
    assert [edge.table for edge in across[TableRef("db", "courses")][:2]] == [
        "enrollments",
        "enrollments",
    ]


def test_students_per_course_count_each_student_once(app):
    result = metrics(app, "db.students", "*:count", dimensions=["courses.title"])
    assert rows(result) == [("Algo", 2), ("Calc", 1), ("DB", 1)]
    assert rows(result) == sql(
        app,
        "SELECT x.title, COUNT(*) "
        + ACROSS.format(cols="c.title", where="")
        + " GROUP BY 1 ORDER BY 1",
    )


def test_a_student_in_two_courses_of_one_department_counts_once_for_it(app):
    result = metrics(app, "db.students", "balance:sum", dimensions=["departments.name"])
    # Ann takes Algo and DB, both CS: 10 once, not twice.
    assert rows(result) == [("CS", 30.0), ("Math", 5.0)]


def test_a_condition_across_the_link_counts_each_student_once(app):
    result = metrics(
        app,
        "db.students",
        "*:count",
        "balance:sum",
        filters=[{"column": "departments.name", "value": "CS"}],
    )
    assert rows(result) == [(2, 30.0)]
    assert rows(result) == sql(
        app,
        "SELECT COUNT(*), SUM(s.balance) "
        + ACROSS.format(cols="d.name", where="WHERE d.name = 'CS'").replace(
            "SELECT DISTINCT e.student_id, d.name", "SELECT DISTINCT e.student_id"
        ),
    )


def test_own_columns_and_the_far_side_together(app):
    result = metrics(app, "db.students", "*:count", dimensions=["tenant", "courses.title"])
    # Ordered by the first breakdown only, so compared as a set of rows.
    assert sorted(rows(result)) == [
        ("a", "Algo", 1),
        ("a", "Calc", 1),
        ("a", "DB", 1),
        ("b", "Algo", 1),
    ]


def test_the_other_way_round(app):
    result = metrics(app, "db.courses", "*:count", dimensions=["students.name"])
    assert rows(result) == [("Ann", 2), ("Bo", 1), ("Cy", 1)]


def test_the_callers_row_rules_reach_both_sides(app):
    rule = [{"column": "tenant", "value": "${tenant}"}]
    scope = AccessScope(
        actor="key-a", tenant="a", policy=build_policy({"students": rule}, caller={"tenant": "a"})
    )
    result = metrics(app, "db.students", "balance:sum", dimensions=["courses.title"], scope=scope)
    assert rows(result) == [("Algo", 10.0), ("Calc", 5.0), ("DB", 10.0)]  # no Bo


def test_a_ranking_across_the_link(app):
    result = metrics(app, "db.students", "rank(*:count)", dimensions=["courses.title"])
    assert rows(result) == [("Algo", 1), ("Calc", 2), ("DB", 2)]


def test_a_running_total_across_the_link_under_the_callers_rules(app):
    rule = [{"column": "tenant", "value": "${tenant}"}]
    scope = AccessScope(
        actor="key-a", tenant="a", policy=build_policy({"students": rule}, caller={"tenant": "a"})
    )
    result = metrics(
        app, "db.students", "cumsum(balance:sum)", dimensions=["departments.name"], scope=scope
    )
    # CS: Ann once (10), Math: Cy (5); Bo is another tenant's.
    assert rows(result) == [("CS", 10.0), ("Math", 15.0)]


def test_one_link_at_a_time(app):
    error = metrics(app, "db.students", "*:count", dimensions=["courses.title", "clubs.name"])[
        "error"
    ]
    assert "one link table at a time" in error
