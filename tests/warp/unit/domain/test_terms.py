"""Business terms: who may read them, and what a reviewer's signature survives."""

import pytest

from truejoin.domain.catalog import catalog_review as rules
from truejoin.domain.catalog.catalog import (
    CatalogStatus,
    ColumnCatalogEntry,
    DatabaseCatalog,
    TableCatalogEntry,
    TableReviewStatus,
)
from truejoin.domain.catalog.terms import (
    ColumnAttribute,
    SqlAttribute,
    Term,
    carry_over,
    clean_words,
)


def term(name: str, *tables: str, certified: bool = False, columns=("status",)) -> Term:
    return Term(
        name=name,
        synonyms=["sale"],
        tables=list(tables),
        column_attributes=[
            ColumnAttribute(table=tables[0], column=c, synonyms=["state"]) for c in columns
        ],
        sql_attributes=[SqlAttribute(name="paid_total", table=tables[0], expression="total")],
        certified=certified,
    )


def catalog(**terms: Term) -> DatabaseCatalog:
    return DatabaseCatalog(
        database_name="db",
        tables={
            name: TableCatalogEntry(
                table_name=name, columns=[ColumnCatalogEntry(name="status", data_type="varchar")]
            )
            for name in ("orders", "items")
        },
        terms=dict(terms),
    )


class TestCleanWords:
    def test_blanks_repeats_and_the_name_itself_are_dropped(self):
        assert clean_words([" Sale ", "sale", "", "ORDER", "deal"], "order") == ["Sale", "deal"]

    @pytest.mark.parametrize("junk", [None, "sale", {"a": 1}, 3])
    def test_anything_that_is_not_a_list_is_no_words(self, junk):
        assert clean_words(junk) == []

    def test_an_entry_that_is_not_a_word_is_skipped(self):
        assert clean_words(["sale", ["nested"], None, 7]) == ["sale", "7"]


class TestWhoMayReadATerm:
    def test_a_draft_catalog_offers_none_however_certified(self):
        c = catalog(Order=term("Order", "orders", certified=True))
        assert c.certified_terms == [] and c.term_for("orders") is None

    def test_an_approved_catalog_offers_only_the_certified_ones(self):
        c = catalog(Order=term("Order", "orders", certified=True), Item=term("Item", "items"))
        c.status = CatalogStatus.approved
        assert [t.name for t in c.certified_terms] == ["Order"]
        assert c.term_for("orders").name == "Order"
        assert c.term_for("items") is None

    def test_an_old_catalog_file_without_terms_still_loads(self):
        assert DatabaseCatalog.model_validate({"database_name": "db"}).terms == {}

    def test_only_certified_expressions_of_the_table_are_offered(self):
        t = term("Order", "orders")
        assert t.certified_sql("orders") == []
        t.sql_attributes[0].certified = True
        assert [a.name for a in t.certified_sql("orders")] == ["paid_total"]
        assert t.certified_sql("items") == []


class TestCertifying:
    def test_approving_a_table_after_showing_its_term_certifies_it(self):
        c = catalog(Order=term("Order", "orders"), Item=term("Item", "items"))
        rules.approve_table(c, "orders", certify_term=True)
        assert c.terms["Order"].certified and not c.terms["Item"].certified

    def test_approving_a_table_alone_certifies_nothing(self):
        c = catalog(Order=term("Order", "orders"))
        rules.approve_table(c, "orders")
        assert not c.terms["Order"].certified

    def test_a_term_over_two_tables_waits_for_both(self):
        c = catalog(Sale=term("Sale", "orders", "items"))
        rules.approve_table(c, "orders", certify_term=True)
        assert not c.terms["Sale"].certified
        rules.approve_table(c, "items", certify_term=True)
        assert c.terms["Sale"].certified

    def test_approving_the_whole_catalog_certifies_nothing(self):
        c = catalog(Order=term("Order", "orders"))
        rules.approve_catalog(c)
        assert c.status == CatalogStatus.approved
        assert c.tables["orders"].review_status == TableReviewStatus.approved
        assert c.certified_terms == []

    def test_certifying_them_all_is_its_own_decision(self):
        c = catalog(Order=term("Order", "orders", certified=True), Item=term("Item", "items"))
        assert rules.certify_terms(c) == 1
        assert all(t.certified for t in c.terms.values())

    def test_an_expression_is_certified_apart_from_its_term(self):
        c = catalog(Order=term("Order", "orders"))
        rules.approve_table(c, "orders")
        assert not c.terms["Order"].sql_attributes[0].certified
        rules.update_term(c, "Order", {"certify_sql": ["paid_total"]})
        assert c.terms["Order"].sql_attributes[0].certified


class TestEditing:
    def test_synonyms_and_column_words_are_cleaned(self):
        c = catalog(Order=term("Order", "orders"))
        rules.update_term(
            c, "Order", {"synonyms": ["deal", " deal ", "Order"], "columns": {"status": ["durum"]}}
        )
        assert c.terms["Order"].synonyms == ["deal"]
        assert c.terms["Order"].attribute("orders", "status").synonyms == ["durum"]

    def test_renaming_moves_the_key_and_keeps_the_order(self):
        c = catalog(Order=term("Order", "orders"), Item=term("Item", "items"))
        rules.update_term(c, "Order", {"name": "Sale"})
        assert list(c.terms) == ["Sale", "Item"] and c.terms["Sale"].name == "Sale"

    @pytest.mark.parametrize(
        ("name", "updates", "message"),
        [
            ("Nope", {}, "No term 'Nope'"),
            ("Order", {"name": "Item"}, "already exists"),
            ("Order", {"columns": {"nope": ["x"]}}, "no column 'nope'"),
            ("Order", {"certify_sql": ["nope"]}, "no expression 'nope'"),
        ],
    )
    def test_what_does_not_exist_is_refused(self, name, updates, message):
        c = catalog(Order=term("Order", "orders"), Item=term("Item", "items"))
        with pytest.raises(KeyError, match=message):
            rules.update_term(c, name, updates)


class TestCarryOver:
    COLUMNS = {"orders": ["status", "total"], "items": ["qty"]}

    def test_a_certified_term_replaces_the_new_guess_for_its_table(self):
        fresh = {"Purchase": term("Purchase", "orders"), "Item": term("Item", "items")}
        kept = carry_over(fresh, [term("Order", "orders", certified=True)], self.COLUMNS)
        assert sorted(kept) == ["Item", "Order"] and kept["Order"].certified

    def test_an_uncertified_one_is_written_again(self):
        fresh = {"Purchase": term("Purchase", "orders")}
        assert list(carry_over(fresh, [term("Order", "orders")], self.COLUMNS)) == ["Purchase"]

    def test_a_term_whose_table_is_gone_goes_with_it(self):
        old = term("Refund", "refunds", certified=True)
        assert carry_over({}, [old], self.COLUMNS) == {}

    def test_a_column_that_is_gone_leaves_the_term(self):
        old = term("Order", "orders", certified=True, columns=("status", "legacy"))
        kept = carry_over({}, [old], self.COLUMNS)["Order"]
        assert [a.column for a in kept.column_attributes] == ["status"]
        assert [a.column for a in old.column_attributes] == ["status", "legacy"]


class TestSuggestedExpressions:
    def draft(self, name: str = "net", table: str = "orders", expression: str = "a - b", **kw):
        return SqlAttribute(name=name, table=table, expression=expression, **kw)

    def test_a_draft_goes_to_the_term_of_its_table(self):
        c = catalog(Order=term("Order", "orders"))
        added = rules.add_sql_attributes(c, [self.draft()])
        assert [a.name for a in added] == ["net"]
        assert [a.name for a in c.terms["Order"].sql_attributes] == ["paid_total", "net"]

    def test_it_arrives_uncertified_whatever_it_claimed(self):
        c = catalog(Order=term("Order", "orders"))
        (added,) = rules.add_sql_attributes(c, [self.draft(certified=True)])
        assert not added.certified

    def test_one_the_term_already_has_or_no_term_covers_is_left_out(self):
        c = catalog(Order=term("Order", "orders"))
        added = rules.add_sql_attributes(
            c,
            [
                self.draft(name="paid_total"),
                self.draft(name="other", expression="total"),
                self.draft(table="items"),
            ],
        )
        assert added == []

    def test_certifying_by_name_leaves_the_review_status_alone(self):
        c = catalog(Order=term("Order", "orders"))
        c.status = CatalogStatus.approved
        rules.add_sql_attributes(c, [self.draft()])
        certified = rules.certify_expressions(c, ["net", "net"])
        assert [a.name for a in certified] == ["net"] and certified[0].certified
        assert c.status == CatalogStatus.approved
        assert not c.terms["Order"].sql_attributes[0].certified

    def test_an_unknown_name_certifies_nothing_and_lists_the_drafts(self):
        c = catalog(Order=term("Order", "orders"))
        rules.add_sql_attributes(c, [self.draft()])
        with pytest.raises(
            KeyError, match=r"No expression called nope .*uncertified: net, paid_total"
        ):
            rules.certify_expressions(c, ["net", "nope"])
        assert not any(a.certified for a in c.terms["Order"].sql_attributes)
