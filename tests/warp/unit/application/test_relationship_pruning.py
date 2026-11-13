"""Relationships a catalog proposes are checked against the data before they count."""

from tests.warp.conftest import MockDatabaseAdapter
from truejoin.application.catalog.relationship_pruning import verify_relationships
from truejoin.domain.catalog.catalog import (
    ColumnCatalogEntry,
    DatabaseCatalog,
    ForeignKeyInfo,
    RelationshipInfo,
    TableCatalogEntry,
)

ROWS = {
    "customers": [{"id": 1}, {"id": 2}, {"id": 3}],
    "orders": [
        {"id": 10, "customer_id": 2, "ref": 900, "note": None},
        {"id": 11, "customer_id": 3, "ref": 901, "note": None},
    ],
}


def gateway() -> MockDatabaseAdapter:
    db = MockDatabaseAdapter()
    for table, rows in ROWS.items():
        db.add_mock_table(table, {"table_name": table}, rows)
    return db


def catalog(*relationships: RelationshipInfo, declared: bool = False) -> DatabaseCatalog:
    def table(name: str) -> TableCatalogEntry:
        columns = [ColumnCatalogEntry(name=c, data_type="integer") for c in ROWS[name][0]]
        return TableCatalogEntry(table_name=name, columns=columns)

    orders = table("orders")
    orders.relationships = list(relationships)
    if declared:
        orders.foreign_keys = [
            ForeignKeyInfo(
                column="customer_id", references_table="customers", references_column="id"
            )
        ]
    return DatabaseCatalog(
        database_name="db", tables={"orders": orders, "customers": table("customers")}
    )


def to_customers(
    column: str, target: str = "customers", target_column: str = "id"
) -> RelationshipInfo:
    return RelationshipInfo(source_column=column, target_table=target, target_column=target_column)


async def verified(relationship: RelationshipInfo, db=None, **kwargs) -> bool | None:
    await verify_relationships(db or gateway(), catalog(relationship, **kwargs))
    return relationship.verified


class TestVerifyRelationships:
    async def test_a_declared_key_needs_no_probe(self):
        db = gateway()
        assert await verified(to_customers("customer_id"), db, declared=True) is True
        assert db.stream_calls == []

    async def test_values_found_in_the_target_bear_it_out(self):
        db = gateway()
        assert await verified(to_customers("customer_id"), db) is True
        sample, lookup = db.stream_calls
        assert (sample["table"], sample["columns"]) == ("orders", ["customer_id"])
        assert lookup["filters"] == [("id", "in", [2, 3])]
        assert lookup["limit"] == 1

    async def test_values_found_nowhere_contradict_it(self):
        relationship = to_customers("ref")
        db = gateway()
        refuted = await verify_relationships(db, catalog(relationship))
        assert relationship.verified is False
        assert refuted == 1

    async def test_an_empty_column_leaves_it_open(self):
        assert await verified(to_customers("note")) is None

    async def test_a_table_or_column_that_is_not_there_contradicts_it(self):
        assert await verified(to_customers("customer_id", target="clients")) is False
        assert await verified(to_customers("customer_id", target_column="uuid")) is False
        assert await verified(to_customers("buyer")) is False

    async def test_a_failed_probe_leaves_it_open(self):
        class Broken(MockDatabaseAdapter):
            def stream_select(self, *args, **kwargs):
                raise OSError("connection lost")

        assert await verified(to_customers("customer_id"), Broken()) is None
