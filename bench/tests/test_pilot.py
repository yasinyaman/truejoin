"""The bench's pilot: gold queries named as the engine names the tables."""

from bench.pilot import qualified, questions


def test_bare_tables_get_the_source_name():
    sql = "SELECT s.sehir FROM islemler i JOIN musteriler m ON i.musteri_id = m.musteri_id"
    assert qualified(sql) == (
        "SELECT s.sehir FROM banka.islemler i JOIN banka.musteriler m ON i.musteri_id = m.musteri_id"
    )


def test_twenty_questions_none_tuned_on():
    asked = questions()
    assert len(asked) == 20 and all(q.id[0] in "hu" for q in asked)
