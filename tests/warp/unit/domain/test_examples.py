"""What counts as a value in a saved analysis: anything a restricted caller must not see."""

import pytest

from trio.domain.catalog.examples import has_literals


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM t WHERE s = 'x'",
        "SELECT * FROM t WHERE tenant_id = 42",
        "SELECT * FROM t WHERE a = 1.5",
        "SELECT 1e3 FROM t",
        "SELECT * FROM t WHERE s = $$acme$$",
        "SELECT * FROM t WHERE s = $q$acme$q$",
        # Conservative: a constant or a number in a quoted name hides it too.
        "SELECT ROUND(x, 2) FROM t",
        'SELECT "col 2" FROM t',
        # A number in a GROUP BY or ORDER BY that is not a bare position.
        "SELECT a FROM t GROUP BY a HAVING count(*) > 100",
        "SELECT a FROM t ORDER BY a = 42",
    ],
)
def test_a_value_is_found(sql: str) -> None:
    assert has_literals(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT count(*) FROM t",
        "SELECT * FROM t2 JOIN t3 ON t2.a1 = t3.a1",
        "SELECT * FROM t LIMIT 10 OFFSET 20",
        "SELECT * FROM t LIMIT 5, 10",
        "SELECT TOP 5 * FROM t",
        "SELECT * FROM t FETCH FIRST 5 ROWS ONLY",
        "SELECT * FROM t WHERE x = $1",
        "SELECT a, count(*) FROM t GROUP BY 1 ORDER BY 2 DESC LIMIT 5",
        "SELECT a, b, count(*) FROM t GROUP BY 1, b ORDER BY 3 DESC NULLS LAST",
        "SELECT * FROM (SELECT a FROM t GROUP BY 1) s",
    ],
)
def test_names_binds_and_row_counts_are_not_values(sql: str) -> None:
    assert not has_literals(sql)
