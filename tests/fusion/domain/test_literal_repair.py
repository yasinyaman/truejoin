"""The proof gate: one literal changed, and nothing else."""

import pytest

from truejoin.domain.olap.literal_repair import prove_substitution, split_literals

ORIGINAL = """SELECT "seg", COUNT(*) FROM db.customers WHERE seg = 'Premium' AND city = 'Izmir'"""


def accept(sql: str) -> None:
    return None


def refuse(sql: str) -> None:
    raise ValueError("not a SELECT")


def candidate(**changes: str) -> str:
    sql = ORIGINAL
    for old, new in changes.items():
        sql = sql.replace(old, new)
    return sql


class TestSplitLiterals:
    def test_literals_come_out_in_order_and_the_rest_is_normalised(self):
        skeleton, literals = split_literals("select  a from t where b = 'x' and c = 'it''s'")
        assert literals == ["x", "it's"]
        assert skeleton == "SELECT A FROM T WHERE B = '?' AND C = '?'"

    def test_a_quoted_identifier_is_part_of_the_statement_not_a_literal(self):
        skeleton, literals = split_literals("""SELECT "it's" FROM t WHERE a = 'b'""")
        assert literals == ["b"] and '"it\'s"' in skeleton


class TestProveSubstitution:
    def test_the_one_literal_swapped_is_proven(self):
        assert prove_substitution(
            ORIGINAL, candidate(Premium="premium"), "Premium", "premium", accept
        )

    def test_whitespace_and_keyword_case_do_not_matter(self):
        rewritten = candidate(Premium="premium").replace("WHERE", "\n  where")
        assert prove_substitution(ORIGINAL, rewritten, "Premium", "premium", accept)

    @pytest.mark.parametrize(
        "changed",
        [
            # The swap was not made.
            ORIGINAL,
            # Another literal moved with it.
            candidate(Premium="premium", Izmir="Ankara"),
            # The statement changed around the literal.
            candidate(Premium="premium", **{"seg =": "seg <>"}),
            candidate(Premium="premium", **{'"seg"': '"email"'}),
            candidate(Premium="premium") + " OR 1 = 1",
            # A condition was dropped along with its literal.
            candidate(Premium="premium", **{" AND city = 'Izmir'": ""}),
        ],
    )
    def test_anything_else_is_not(self, changed):
        assert not prove_substitution(ORIGINAL, changed, "Premium", "premium", accept)

    def test_a_literal_the_original_never_had_proves_nothing(self):
        assert not prove_substitution(ORIGINAL, ORIGINAL, "Gold", "gold", accept)
        assert not prove_substitution(ORIGINAL, ORIGINAL, "Premium", "Premium", accept)

    def test_every_occurrence_has_to_change(self):
        twice = ORIGINAL + " OR seg2 = 'Premium'"
        assert prove_substitution(
            twice, twice.replace("Premium", "premium"), "Premium", "premium", accept
        )
        assert not prove_substitution(
            twice, twice.replace("Premium", "premium", 1), "Premium", "premium", accept
        )

    def test_a_candidate_the_guardrails_refuse_is_not_proven(self):
        assert not prove_substitution(
            ORIGINAL, candidate(Premium="premium"), "Premium", "premium", refuse
        )

    def test_a_literal_that_smuggles_a_quote_cannot_close_itself(self):
        hostile = candidate(Premium="x' OR '1'='1")
        assert not prove_substitution(ORIGINAL, hostile, "Premium", "x' OR '1'='1", accept)
