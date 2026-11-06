"""Pure review-workflow transitions on a `DatabaseCatalog`.

Every function mutates the catalog it is given and returns it (or the touched
entry) without touching storage; the application service loads, applies and
persists.
"""

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from trio.domain.catalog.catalog import (
    CatalogStatus,
    ColumnCatalogEntry,
    DatabaseCatalog,
    RelationshipInfo,
    TableCatalogEntry,
    TableReviewStatus,
)
from trio.domain.catalog.terms import SqlAttribute, Term, clean_words
from trio.domain.gateway.errors import ColumnNotFoundInCatalogError, TableNotFoundInCatalogError

_REVIEWED = (TableReviewStatus.approved, TableReviewStatus.modified)


def _touch(catalog: DatabaseCatalog) -> None:
    catalog.updated_at = datetime.now(UTC)


def _require_table(catalog: DatabaseCatalog, table_name: str) -> TableCatalogEntry:
    table = catalog.get_table(table_name)
    if table is None:
        raise TableNotFoundInCatalogError(table_name, catalog.database_name)
    return table


def mark_as_draft(catalog: DatabaseCatalog) -> DatabaseCatalog:
    """Reset a catalog to draft with every table pending review."""
    catalog.status = CatalogStatus.draft
    for table in catalog.tables.values():
        table.review_status = TableReviewStatus.pending
    _touch(catalog)
    return catalog


def approve_table(
    catalog: DatabaseCatalog, table_name: str, certify_term: bool = False
) -> DatabaseCatalog:
    """Mark one table as approved.

    ``certify_term`` is for a caller that has just shown the reviewer the
    table's term (the CLI walk); the HTTP review shows no terms, so approving
    there certifies none.

    Raises:
        TableNotFoundInCatalogError: If the table is not in the catalog.
    """
    _require_table(catalog, table_name).review_status = TableReviewStatus.approved
    if not certify_term:
        _touch(catalog)
        return catalog
    # A term that covers several tables waits for the last of them.
    for term in catalog.terms.values():
        if table_name in term.tables and all(
            (covered := catalog.get_table(name)) is not None and covered.review_status in _REVIEWED
            for name in term.tables
        ):
            term.certified = True
    _touch(catalog)
    return catalog


def certify_terms(catalog: DatabaseCatalog) -> int:
    """Certify every term nobody certified yet; returns how many.

    Approving a whole catalog does not do this: a term decides what a
    question means, so it is certified by a reviewer who saw it or by someone
    who says, in so many words, that all of them are right.
    """
    pending = [term for term in catalog.terms.values() if not term.certified]
    for term in pending:
        term.certified = True
    _touch(catalog)
    return len(pending)


def add_sql_attributes(
    catalog: DatabaseCatalog, attributes: Iterable[SqlAttribute]
) -> list[SqlAttribute]:
    """Attach draft expressions to the terms that cover their tables; returns those added.

    Always as drafts, whatever the caller set: an expression is certified by a
    reviewer and by nothing else. One whose table no term covers, or that the
    term already has (by name or by expression), is left out.
    """
    added = []
    for attribute in attributes:
        term = next((t for t in catalog.terms.values() if attribute.table in t.tables), None)
        if term is None or any(
            existing.table == attribute.table
            and (existing.name == attribute.name or existing.expression == attribute.expression)
            for existing in term.sql_attributes
        ):
            continue
        draft = attribute.model_copy(update={"certified": False})
        term.sql_attributes.append(draft)
        added.append(draft)
    if added:
        _touch(catalog)
    return added


def certify_expressions(catalog: DatabaseCatalog, names: Iterable[str]) -> list[SqlAttribute]:
    """Certify the expressions called ``names``, in whichever term holds each.

    Raises:
        KeyError: A name no term has, listing the drafts there are. Nothing
            is certified in that case.
    """
    wanted = list(dict.fromkeys(names))
    held = {a.name: a for term in catalog.terms.values() for a in term.sql_attributes}
    missing = [name for name in wanted if name not in held]
    if missing:
        drafts = ", ".join(sorted(n for n, a in held.items() if not a.certified)) or "none"
        raise KeyError(
            f"No expression called {', '.join(missing)} in catalog "
            f"'{catalog.database_name}' (uncertified: {drafts})"
        )
    for name in wanted:
        held[name].certified = True
    _touch(catalog)
    return [held[name] for name in wanted]


def _require_term(catalog: DatabaseCatalog, name: str) -> Term:
    term = catalog.terms.get(name)
    if term is None:
        known = ", ".join(sorted(catalog.terms)) or "none"
        raise KeyError(f"No term '{name}' in catalog '{catalog.database_name}' (terms: {known})")
    return term


def update_term(catalog: DatabaseCatalog, name: str, updates: dict[str, Any]) -> Term:
    """Apply a reviewer's edits to a term.

    ``updates`` may carry ``name`` (renames it), ``synonyms``, ``columns``
    (``{column: [words]}``, for the term's first table) and ``certify_sql``
    (names of its expressions to certify).

    Raises:
        KeyError: No such term, the new name is taken, or ``certify_sql``
            names an expression the term does not have.
    """
    term = _require_term(catalog, name)
    if "synonyms" in updates:
        term.synonyms = clean_words(updates["synonyms"], term.name)
    for column, words in (updates.get("columns") or {}).items():
        attribute = next((a for a in term.column_attributes if a.column == column), None)
        if attribute is None:
            raise KeyError(f"Term '{name}' has no column '{column}'")
        attribute.synonyms = clean_words(words, column)
    for wanted in updates.get("certify_sql") or ():
        expression = next((a for a in term.sql_attributes if a.name == wanted), None)
        if expression is None:
            raise KeyError(f"Term '{name}' has no expression '{wanted}'")
        expression.certified = True
    new_name = str(updates.get("name") or "").strip()
    if new_name and new_name != name:
        if new_name in catalog.terms:
            raise KeyError(f"A term called '{new_name}' already exists")
        term.name = new_name
        catalog.terms = {(new_name if k == name else k): t for k, t in catalog.terms.items()}
    _touch(catalog)
    return term


def approve_catalog(catalog: DatabaseCatalog) -> DatabaseCatalog:
    """Approve every pending table and finalize the catalog."""
    for table in catalog.tables.values():
        if table.review_status == TableReviewStatus.pending:
            table.review_status = TableReviewStatus.approved
    catalog.status = CatalogStatus.approved
    _touch(catalog)
    return catalog


def _set_localized(entry: Any, field_name: str, value: Any, lang: str) -> None:
    localized = getattr(entry, field_name)
    if isinstance(value, str):
        localized.set(lang, value)
        entry.user_overrides.setdefault(field_name, {})[lang] = value
    elif isinstance(value, dict):
        for lang_code, text in value.items():
            localized.set(lang_code, text)
        entry.user_overrides[field_name] = {**entry.user_overrides.get(field_name, {}), **value}


def update_table_fields(
    catalog: DatabaseCatalog, table_name: str, updates: dict[str, Any], lang: str = "en"
) -> TableCatalogEntry:
    """Apply user edits (description, human_name, tags, relationships) to a table.

    The edits are recorded in `user_overrides` so they survive re-analysis and
    the table is marked `modified`.
    """
    table = _require_table(catalog, table_name)

    if "description" in updates:
        _set_localized(table, "description", updates["description"], lang)
    if "human_name" in updates:
        _set_localized(table, "human_name", updates["human_name"], lang)
    if "tags" in updates:
        table.tags = updates["tags"]
        table.user_overrides["tags"] = updates["tags"]
    if "relationships" in updates:
        table.relationships = _with_verdicts(table.relationships, updates["relationships"])
        table.user_overrides["relationships"] = updates["relationships"]

    table.review_status = TableReviewStatus.modified
    _touch(catalog)
    return table


def update_column_fields(
    catalog: DatabaseCatalog,
    table_name: str,
    column_name: str,
    updates: dict[str, Any],
    lang: str = "en",
) -> ColumnCatalogEntry:
    """Apply user edits (description, semantic_type, tags) to a column.

    Raises:
        ColumnNotFoundInCatalogError: If the column is not in the table.
    """
    table = _require_table(catalog, table_name)
    column = table.get_column(column_name)
    if column is None:
        raise ColumnNotFoundInCatalogError(column_name, table_name, catalog.database_name)

    if "description" in updates:
        _set_localized(column, "description", updates["description"], lang)
    if "semantic_type" in updates:
        column.semantic_type = updates["semantic_type"]
        column.user_overrides["semantic_type"] = updates["semantic_type"]
    if "tags" in updates:
        column.tags = updates["tags"]
        column.user_overrides["tags"] = updates["tags"]

    table.review_status = TableReviewStatus.modified
    _touch(catalog)
    return column


def extract_overrides(catalog: DatabaseCatalog) -> dict[str, dict[str, Any]]:
    """Collect every `user_overrides` entry, keyed by table (columns nested)."""
    overrides: dict[str, dict[str, Any]] = {}
    for tname, table in catalog.tables.items():
        table_overrides: dict[str, Any] = dict(table.user_overrides)
        col_overrides = {c.name: dict(c.user_overrides) for c in table.columns if c.user_overrides}
        if col_overrides:
            table_overrides["columns"] = col_overrides
        if table_overrides:
            overrides[tname] = table_overrides
    return overrides


def apply_overrides(  # noqa: C901, PLR0912
    catalog: DatabaseCatalog, overrides: dict[str, dict[str, Any]]
) -> DatabaseCatalog:
    """Re-apply saved user edits on top of a freshly generated catalog.

    Tables/columns that no longer exist are skipped; edited tables become
    `modified`.
    """
    for tname, table_overrides in overrides.items():
        table = catalog.get_table(tname)
        if table is None:
            continue

        for field_name in ("description", "human_name"):
            values = table_overrides.get(field_name)
            if isinstance(values, dict):
                for lang, text in values.items():
                    getattr(table, field_name).set(lang, text)
        if "tags" in table_overrides:
            table.tags = table_overrides["tags"]
        if table_overrides.get("relationships"):
            table.relationships = _with_verdicts(
                table.relationships, table_overrides["relationships"]
            )

        table.user_overrides = {k: v for k, v in table_overrides.items() if k != "columns"}
        if any(k != "columns" for k in table_overrides):
            table.review_status = TableReviewStatus.modified

        for col_name, col_updates in table_overrides.get("columns", {}).items():
            column = table.get_column(col_name)
            if column is None:
                continue
            desc = col_updates.get("description")
            if isinstance(desc, dict):
                for lang, text in desc.items():
                    column.description.set(lang, text)
            if "semantic_type" in col_updates:
                column.semantic_type = col_updates["semantic_type"]
            if "tags" in col_updates:
                column.tags = col_updates["tags"]
            column.user_overrides = dict(col_updates)

    _touch(catalog)
    return catalog


def _with_verdicts(current: list[RelationshipInfo], given: list[Any]) -> list[RelationshipInfo]:
    """``given`` as relationships, each keeping the data's verdict on the one it repeats.

    A reviewer edits what the review shows, which has no ``verified``; without
    this an unchanged relationship would lose a verdict the probe gave it.
    """
    verdicts = {(r.source_column, r.target_table, r.target_column): r.verified for r in current}
    built = []
    for r in given:
        relationship = RelationshipInfo(**r) if isinstance(r, dict) else r
        if not (isinstance(r, dict) and "verified" in r):
            key = (
                relationship.source_column,
                relationship.target_table,
                relationship.target_column,
            )
            relationship.verified = verdicts.get(key, relationship.verified)
        built.append(relationship)
    return built
