"""Catalog review workflow: draft -> edit/approve -> approved, on top of a repository."""

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from truejoin.domain.catalog import catalog_review as rules
from truejoin.domain.catalog.catalog import (
    CatalogIndex,
    ColumnCatalogEntry,
    DatabaseCatalog,
    TableCatalogEntry,
)
from truejoin.domain.catalog.examples import ExampleError, QueryExample
from truejoin.domain.catalog.localized import LocalizedText
from truejoin.domain.catalog.terms import MeasureError, SqlAttribute, Term
from truejoin.domain.gateway.sql_types import type_kind
from truejoin.ports.catalog_repository import CatalogRepository
from truejoin.ports.query_history import ExpressionChecker
from truejoin.ports.sql_policy import SqlAnalyzer, SqlValidator


class CatalogReviewService:
    """Load -> apply a pure transition -> save."""

    def __init__(self, repository: CatalogRepository):
        """Wrap `repository`."""
        self.repository = repository

    # -- persistence pass-throughs -------------------------------------------------
    def load(self, db_name: str) -> DatabaseCatalog | None:
        """Load a catalog, or None."""
        return self.repository.load(db_name)

    def load_or_raise(self, db_name: str) -> DatabaseCatalog:
        """Load a catalog or raise `CatalogNotFoundError`."""
        return self.repository.load_or_raise(db_name)

    def list_catalogs(self) -> list[str]:
        """Names of stored catalogs."""
        return self.repository.list_catalogs()

    def get_index(self) -> CatalogIndex:
        """Summary index of stored catalogs."""
        return self.repository.get_index()

    def delete(self, db_name: str) -> bool:
        """Delete a catalog."""
        return self.repository.delete(db_name)

    # -- transitions ------------------------------------------------------------------
    def save_as_draft(
        self, catalog: DatabaseCatalog, format: str | None = None, keep: Iterable[str] = ()
    ) -> Path:
        """Reset to draft (all tables but ``keep`` pending) and persist."""
        return self.repository.save(rules.mark_as_draft(catalog, keep), format=format)

    def approve_table(
        self, db_name: str, table_name: str, certify_term: bool = False
    ) -> DatabaseCatalog:
        """Approve one table and persist; ``certify_term`` as in the rule."""
        catalog = rules.approve_table(
            self.repository.load_or_raise(db_name), table_name, certify_term
        )
        self.repository.save(catalog)
        return catalog

    def approve_catalog(self, db_name: str) -> DatabaseCatalog:
        """Approve every pending table, finalize and persist."""
        catalog = rules.approve_catalog(self.repository.load_or_raise(db_name))
        self.repository.save(catalog)
        return catalog

    def certify_terms(self, db_name: str) -> int:
        """Certify every uncertified term and persist; returns how many."""
        catalog = self.repository.load_or_raise(db_name)
        certified = rules.certify_terms(catalog)
        self.repository.save(catalog)
        return certified

    def add_sql_attributes(
        self, db_name: str, attributes: Iterable[SqlAttribute]
    ) -> list[SqlAttribute]:
        """Attach draft expressions to their terms and persist; returns those added."""
        catalog = self.repository.load_or_raise(db_name)
        added = rules.add_sql_attributes(catalog, attributes)
        self.repository.save(catalog)
        return added

    def certify_expressions(self, db_name: str, names: Iterable[str]) -> list[SqlAttribute]:
        """Certify expressions by name and persist."""
        catalog = self.repository.load_or_raise(db_name)
        certified = rules.certify_expressions(catalog, names)
        self.repository.save(catalog)
        return certified

    def check_measure(
        self, db_name: str, table: str, expression: str, checker: ExpressionChecker
    ) -> tuple[str, tuple[str, ...]]:
        """``expression`` as it would be stored for ``table``, and the columns it reads.

        Raises:
            MeasureError: The table is not in the catalog, or the expression
                cannot be a measure over it.
        """
        entry = self.repository.load_or_raise(db_name).get_table(table)
        if entry is None:
            raise MeasureError(f"'{table}' is not a table of the catalog")
        kinds = {c.name: type_kind(c.data_type, None, c.full_type) for c in entry.columns}
        return checker.check(expression, kinds)

    def add_measure(  # noqa: PLR0913
        self,
        db_name: str,
        term_name: str,
        *,
        table: str,
        name: str,
        expression: str,
        description: str = "",
        lang: str = "en",
        checker: ExpressionChecker,
    ) -> SqlAttribute:
        """Check a measure a reviewer wrote, attach it to its term as a draft, persist.

        Raises:
            KeyError: No such term.
            MeasureError: The expression or the name cannot be used.
        """
        checked, _ = self.check_measure(db_name, table, expression, checker)
        catalog = self.repository.load_or_raise(db_name)
        text = description.strip()
        measure = rules.add_measure(
            catalog,
            term_name,
            SqlAttribute(
                name=name.strip(),
                table=table,
                expression=checked,
                description=LocalizedText(texts={lang: text} if text else {}),
            ),
        )
        self.repository.save(catalog)
        return measure

    def set_guidance(
        self,
        db_name: str,
        glossary: dict[str, str] | None = None,
        notes: list[str] | None = None,
    ) -> DatabaseCatalog:
        """Replace the glossary and/or the notes, and persist."""
        catalog = self.repository.load_or_raise(db_name)
        rules.set_guidance(catalog, glossary, notes)
        self.repository.save(catalog)
        return catalog

    def add_example(  # noqa: PLR0913
        self,
        db_name: str,
        *,
        name: str,
        question: str,
        sql: str,
        description: str = "",
        lang: str = "en",
        validator: SqlValidator,
        analyzer: SqlAnalyzer,
    ) -> QueryExample:
        """Check an example's SQL as the engine would, and save it as a draft.

        It has to pass the engine's guardrails (one read-only query, no file
        or network function) and read this catalog's tables only, named as
        ``query_data`` takes them (``db_name.table``).

        Raises:
            ExampleError: Why it cannot be an example.
        """
        catalog = self.repository.load_or_raise(db_name)
        sql = sql.strip().rstrip(";").strip()
        if not question.strip():
            raise ExampleError("An example needs the question it answers")
        try:
            validator.validate(sql)
        except Exception as e:
            raise ExampleError(f"Not a query this server runs: {e}") from e
        refs = analyzer.table_references(sql)
        if not refs:
            raise ExampleError("It reads no table")
        strangers = sorted(
            {
                f"{ref.source}.{ref.table}" if ref.source else ref.table
                for ref in refs
                if ref.source != db_name or ref.table not in catalog.tables
            }
        )
        if strangers:
            first = next(iter(catalog.tables), "table")
            raise ExampleError(
                f"{', '.join(strangers)}: not a table of this catalog. Name each one as "
                f"query_data does, e.g. {db_name}.{first}"
            )
        example = rules.add_example(
            catalog,
            QueryExample(
                name=name.strip(),
                question=LocalizedText(texts={lang: question.strip()}),
                sql=sql,
                description=LocalizedText(texts={lang: description.strip()})
                if description.strip()
                else LocalizedText(),
                tables=sorted({f"{ref.source}.{ref.table}" for ref in refs}),
            ),
        )
        self.repository.save(catalog)
        return example

    def certify_example(self, db_name: str, name: str, certified: bool = True) -> QueryExample:
        """Certify an example (or take it back) and persist."""
        catalog = self.repository.load_or_raise(db_name)
        example = rules.certify_example(catalog, name, certified)
        self.repository.save(catalog)
        return example

    def remove_example(self, db_name: str, name: str) -> QueryExample:
        """Remove an example and persist."""
        catalog = self.repository.load_or_raise(db_name)
        removed = rules.remove_example(catalog, name)
        self.repository.save(catalog)
        return removed

    def import_catalog(self, catalog: DatabaseCatalog) -> DatabaseCatalog:
        """Store a catalog from elsewhere as an unreviewed draft."""
        rules.as_unreviewed(catalog)
        self.repository.save(catalog)
        return catalog

    def set_owners(
        self,
        db_name: str,
        tables: dict[str, str] | None = None,
        terms: dict[str, str] | None = None,
    ) -> DatabaseCatalog:
        """Set or clear owners of tables and terms, and persist."""
        catalog = self.repository.load_or_raise(db_name)
        rules.set_owners(catalog, tables, terms)
        self.repository.save(catalog)
        return catalog

    def tags(self, db_name: str) -> list[dict[str, Any]]:
        """Every tag of a catalog and what carries it."""
        return rules.tags_of(self.repository.load_or_raise(db_name))

    def tag_targets(
        self,
        db_name: str,
        tag: str,
        targets: Iterable[tuple[str, str | None]],
        remove: bool = False,
    ) -> int:
        """Put a tag on (or take it off) tables and columns, and persist; returns how many changed."""
        catalog = self.repository.load_or_raise(db_name)
        changed = rules.tag_targets(catalog, tag, targets, remove)
        if changed:
            self.repository.save(catalog)
        return changed

    def rename_tag(self, db_name: str, old: str, new: str) -> int:
        """Rename a tag everywhere and persist; returns how many carried it."""
        catalog = self.repository.load_or_raise(db_name)
        changed = rules.rename_tag(catalog, old, new)
        if changed:
            self.repository.save(catalog)
        return changed

    def remove_tag(self, db_name: str, tag: str) -> int:
        """Take a tag off everything and persist; returns how many carried it."""
        catalog = self.repository.load_or_raise(db_name)
        changed = rules.remove_tag(catalog, tag)
        if changed:
            self.repository.save(catalog)
        return changed

    def remove_measure(self, db_name: str, term_name: str, name: str) -> SqlAttribute:
        """Remove an expression from its term and persist."""
        catalog = self.repository.load_or_raise(db_name)
        removed = rules.remove_measure(catalog, term_name, name)
        self.repository.save(catalog)
        return removed

    def update_term(self, db_name: str, term_name: str, updates: dict[str, Any]) -> Term:
        """Apply term edits and persist."""
        catalog = self.repository.load_or_raise(db_name)
        term = rules.update_term(catalog, term_name, updates)
        self.repository.save(catalog)
        return term

    def update_table_fields(
        self, db_name: str, table_name: str, updates: dict[str, Any], lang: str = "en"
    ) -> TableCatalogEntry:
        """Apply table edits and persist."""
        catalog = self.repository.load_or_raise(db_name)
        table = rules.update_table_fields(catalog, table_name, updates, lang)
        self.repository.save(catalog)
        return table

    def update_column_fields(
        self,
        db_name: str,
        table_name: str,
        column_name: str,
        updates: dict[str, Any],
        lang: str = "en",
    ) -> ColumnCatalogEntry:
        """Apply column edits and persist."""
        catalog = self.repository.load_or_raise(db_name)
        column = rules.update_column_fields(catalog, table_name, column_name, updates, lang)
        self.repository.save(catalog)
        return column

    def extract_overrides(self, db_name: str) -> dict[str, dict[str, Any]]:
        """User edits of an existing catalog (empty when none is stored)."""
        catalog = self.repository.load(db_name)
        return rules.extract_overrides(catalog) if catalog else {}

    def apply_overrides(
        self, db_name: str, overrides: dict[str, dict[str, Any]]
    ) -> DatabaseCatalog:
        """Re-apply saved user edits and persist."""
        catalog = rules.apply_overrides(self.repository.load_or_raise(db_name), overrides)
        self.repository.save(catalog)
        return catalog
