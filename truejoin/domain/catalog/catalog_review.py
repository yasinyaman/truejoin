"""Pure review-workflow transitions on a `DatabaseCatalog`.

Every function mutates the catalog it is given and returns it (or the touched
entry) without touching storage; the application service loads, applies and
persists.
"""

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

from truejoin.domain.catalog.catalog import (
    CatalogStatus,
    ColumnCatalogEntry,
    DatabaseCatalog,
    EditMark,
    RelationshipInfo,
    TableCatalogEntry,
    TableReviewStatus,
)
from truejoin.domain.catalog.examples import ExampleError, QueryExample
from truejoin.domain.catalog.terms import MeasureError, SqlAttribute, Term, clean_words
from truejoin.domain.gateway.errors import ColumnNotFoundInCatalogError, TableNotFoundInCatalogError
from truejoin.domain.gateway.samples import looks_personal
from truejoin.domain.olap.identifiers import VIEW_NAME_RE

_REVIEWED = (TableReviewStatus.approved, TableReviewStatus.modified)


def _touch(catalog: DatabaseCatalog) -> None:
    catalog.updated_at = datetime.now(UTC)


def _require_table(catalog: DatabaseCatalog, table_name: str) -> TableCatalogEntry:
    table = catalog.get_table(table_name)
    if table is None:
        raise TableNotFoundInCatalogError(table_name, catalog.database_name)
    return table


def mark_as_draft(catalog: DatabaseCatalog, keep: Iterable[str] = ()) -> DatabaseCatalog:
    """Reset a catalog to draft with every table pending review.

    Tables in ``keep`` were carried over unchanged, so their review stands.
    """
    catalog.status = CatalogStatus.draft
    kept = set(keep)
    for name, table in catalog.tables.items():
        if name not in kept:
            table.review_status = TableReviewStatus.pending
    _touch(catalog)
    return catalog


def add_example(catalog: DatabaseCatalog, example: QueryExample) -> QueryExample:
    """Save an example as a draft: its SQL already checked, its name free.

    Raises:
        ExampleError: The name is not an identifier, or is taken.
    """
    if not VIEW_NAME_RE.match(example.name):
        raise ExampleError(
            f"'{example.name}' is not a usable name: letters, digits and '_', "
            f"starting with a letter or '_'"
        )
    if example.name in catalog.examples:
        raise ExampleError(f"'{example.name}' is already an example of this catalog")
    draft = example.model_copy(update={"certified": False})
    catalog.examples[example.name] = draft
    _touch(catalog)
    return draft


def certify_example(catalog: DatabaseCatalog, name: str, certified: bool = True) -> QueryExample:
    """Certify an example (or take the certification back).

    Raises:
        KeyError: No such example.
    """
    example = catalog.examples.get(name)
    if example is None:
        known = ", ".join(sorted(catalog.examples)) or "none"
        raise KeyError(f"No example '{name}' in '{catalog.database_name}' (examples: {known})")
    example.certified = certified
    _touch(catalog)
    return example


def remove_example(catalog: DatabaseCatalog, name: str) -> QueryExample:
    """Remove an example, draft or certified.

    Raises:
        KeyError: No such example.
    """
    if name not in catalog.examples:
        raise KeyError(f"No example '{name}' in '{catalog.database_name}'")
    removed = catalog.examples.pop(name)
    _touch(catalog)
    return removed


def as_unreviewed(catalog: DatabaseCatalog) -> DatabaseCatalog:
    """A catalog nobody here has reviewed: a draft, every table pending, nothing certified.

    For one brought in from elsewhere: its approvals and certifications were
    someone else's, and what goes live here is decided here.
    """
    mark_as_draft(catalog)
    for term in catalog.terms.values():
        term.certified = False
        for measure in term.sql_attributes:
            measure.certified = False
    for example in catalog.examples.values():
        example.certified = False
    return catalog


def approve_table(
    catalog: DatabaseCatalog, table_name: str, certify_term: bool = False
) -> DatabaseCatalog:
    """Mark one table as approved.

    ``certify_term`` is for a caller that has just shown the reviewer the
    table's terms: the CLI walk, and the console's table review, which lists
    them. Approving a table that is already approved, without certifying,
    changes nothing, not even the time: a new time would serve the catalog again.

    Raises:
        TableNotFoundInCatalogError: If the table is not in the catalog.
    """
    table = _require_table(catalog, table_name)
    if not certify_term and table.review_status == TableReviewStatus.approved:
        return catalog
    table.review_status = TableReviewStatus.approved
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
    term already has (by name, or the same expression with the same
    aggregation), is left out.
    """
    added = []
    for attribute in attributes:
        term = next((t for t in catalog.terms.values() if attribute.table in t.tables), None)
        if term is None or any(
            existing.table == attribute.table
            and (
                existing.name == attribute.name
                or (
                    existing.expression == attribute.expression
                    and existing.aggregation == attribute.aggregation
                )
            )
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


def add_measure(catalog: DatabaseCatalog, term_name: str, measure: SqlAttribute) -> SqlAttribute:
    """Attach a measure a reviewer wrote to ``term_name``, as a draft.

    The expression is checked before this (``ExpressionChecker``); here only
    where it goes and what it is called: a table the term covers, a name that
    is an identifier, free on that table (no column and no other expression
    has it), and an expression the term does not hold already with the same
    aggregation (the sum and the average of one expression are two measures).
    Changing a measure's aggregation is removing it and adding another, so the
    new one is a draft: a certification never survives a change of meaning.

    Raises:
        KeyError: No such term.
        MeasureError: Any of the above, saying which.
    """
    term = _require_term(catalog, term_name)
    if measure.table not in term.tables:
        raise MeasureError(
            f"Term '{term.name}' covers {', '.join(term.tables) or 'no table'}, "
            f"not '{measure.table}'"
        )
    # The OLAP side offers an expression as a measure only under such a name.
    if not VIEW_NAME_RE.match(measure.name):
        raise MeasureError(
            f"'{measure.name}' is not a usable name: letters, digits and '_', "
            f"starting with a letter or '_'"
        )
    table = catalog.get_table(measure.table)
    taken = {c.lower() for c in table.get_column_names()} if table is not None else set()
    for existing in term.sql_attributes:
        if existing.table != measure.table:
            continue
        taken.add(existing.name.lower())
        if (
            existing.expression == measure.expression
            and existing.aggregation == measure.aggregation
        ):
            raise MeasureError(
                f"'{existing.name}' already is the {measure.aggregation} of this expression"
            )
    if measure.name.lower() in taken:
        raise MeasureError(f"'{measure.name}' is already taken on {measure.table}")
    draft = measure.model_copy(update={"certified": False, "source_sql_ids": []})
    term.sql_attributes.append(draft)
    _touch(catalog)
    return draft


def set_guidance(
    catalog: DatabaseCatalog,
    glossary: dict[str, str] | None = None,
    notes: list[str] | None = None,
) -> DatabaseCatalog:
    """Replace what is given of the glossary and the notes, trimmed; blanks are dropped."""
    if glossary is not None:
        catalog.glossary = {
            short.strip(): long.strip()
            for short, long in glossary.items()
            if short.strip() and long.strip() and short.strip() != long.strip()
        }
    if notes is not None:
        catalog.notes = [note.strip() for note in notes if note.strip()]
    _touch(catalog)
    return catalog


def remove_measure(catalog: DatabaseCatalog, term_name: str, name: str) -> SqlAttribute:
    """Take the expression called ``name`` off ``term_name``, draft or measure.

    Raises:
        KeyError: No such term, or the term has no such expression.
    """
    term = _require_term(catalog, term_name)
    found = next((a for a in term.sql_attributes if a.name == name), None)
    if found is None:
        raise KeyError(f"Term '{term_name}' has no expression '{name}'")
    term.sql_attributes.remove(found)
    _touch(catalog)
    return found


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


#: Where each localized field keeps what the model wrote for it.
_GENERATED = {"description": "generated_description", "human_name": "generated_human_name"}


def _set_localized(entry: Any, field_name: str, value: Any, lang: str, by: str | None) -> None:
    """Set a localized field and record it as an edit, language by language.

    Setting a language back to what the model wrote for it is no edit: its
    override goes, and with the last of them the field's edit mark.
    """
    localized = getattr(entry, field_name)
    texts = (
        {lang: value} if isinstance(value, str) else dict(value) if isinstance(value, dict) else {}
    )
    generated = getattr(entry, _GENERATED.get(field_name, ""), None)
    overrides = dict(entry.user_overrides.get(field_name, {}))
    for lang_code, text in texts.items():
        localized.set(lang_code, text)
        if generated is not None and generated.texts.get(lang_code) == text:
            overrides.pop(lang_code, None)
        else:
            overrides[lang_code] = text
    _mark(entry, field_name, overrides or None, by)


def _mark(entry: Any, field_name: str, override: Any, by: str | None) -> None:
    """Keep ``override`` as the field's edit and say who made it; ``None`` takes both back."""
    if override is None:
        entry.user_overrides.pop(field_name, None)
        entry.edited.pop(field_name, None)
    else:
        entry.user_overrides[field_name] = override
        entry.edited[field_name] = EditMark(by=by, at=datetime.now(UTC))


def update_table_fields(
    catalog: DatabaseCatalog,
    table_name: str,
    updates: dict[str, Any],
    lang: str = "en",
    by: str | None = None,
) -> TableCatalogEntry:
    """Apply user edits (description, human_name, tags, relationships) to a table.

    The edits are recorded in `user_overrides` so they survive re-analysis,
    each with who made it (``by``) in `edited`, and the table is marked
    `modified`.
    """
    table = _require_table(catalog, table_name)

    if "description" in updates:
        _set_localized(table, "description", updates["description"], lang, by)
    if "human_name" in updates:
        _set_localized(table, "human_name", updates["human_name"], lang, by)
    if "tags" in updates:
        table.tags = updates["tags"]
        _mark(table, "tags", updates["tags"], by)
    if "relationships" in updates:
        table.relationships = _with_verdicts(table.relationships, updates["relationships"])
        _mark(table, "relationships", updates["relationships"], by)

    table.review_status = TableReviewStatus.modified
    _touch(catalog)
    return table


def update_column_fields(
    catalog: DatabaseCatalog,
    table_name: str,
    column_name: str,
    updates: dict[str, Any],
    lang: str = "en",
    by: str | None = None,
) -> ColumnCatalogEntry:
    """Apply user edits (description, semantic_type, tags) to a column, as ``by``.

    Raises:
        ColumnNotFoundInCatalogError: If the column is not in the table.
    """
    table = _require_table(catalog, table_name)
    column = table.get_column(column_name)
    if column is None:
        raise ColumnNotFoundInCatalogError(column_name, table_name, catalog.database_name)

    if "description" in updates:
        _set_localized(column, "description", updates["description"], lang, by)
    if "semantic_type" in updates:
        # A cleared field is no type, not the type "": masking matches it exactly.
        semantic_type = (updates["semantic_type"] or "").strip() or None
        column.semantic_type = semantic_type
        back = (
            column.generated_semantic_type is not None
            and semantic_type == column.generated_semantic_type
        )
        if back:  # the model's own type again: no edit
            _mark(column, "semantic_type", None, by)
        else:
            column.user_overrides["semantic_type"] = semantic_type
            column.edited["semantic_type"] = EditMark(by=by, at=datetime.now(UTC))
    if "tags" in updates:
        column.tags = updates["tags"]
        _mark(column, "tags", updates["tags"], by)
    # A reviewer who marks a column personal takes back what analysis kept of it.
    if looks_personal(column.name, column.semantic_type, column.tags):
        column.sample_values = []
        for term in catalog.terms.values():
            for attribute in term.column_attributes:
                if attribute.table == table_name and attribute.column == column_name:
                    attribute.values = {}

    table.review_status = TableReviewStatus.modified
    _touch(catalog)
    return column


def set_owners(
    catalog: DatabaseCatalog,
    tables: dict[str, str] | None = None,
    terms: dict[str, str] | None = None,
) -> DatabaseCatalog:
    """Say who answers for tables and terms; an empty owner removes one.

    Raises:
        KeyError: A table or a term the catalog does not have; nothing is
            changed then.
    """
    unknown = [t for t in tables or {} if t not in catalog.tables]
    unknown += [n for n in terms or {} if n not in catalog.terms]
    if unknown:
        raise KeyError(f"Not in catalog '{catalog.database_name}': {', '.join(unknown)}")
    for owners, given in ((catalog.table_owners, tables), (catalog.term_owners, terms)):
        for name, who in (given or {}).items():
            if who.strip():
                owners[name] = who.strip()
            else:
                owners.pop(name, None)
    _touch(catalog)
    return catalog


def tags_of(catalog: DatabaseCatalog) -> list[dict[str, Any]]:
    """Every tag of the catalog, with the tables and the ``table.column``s that carry it."""
    found: dict[str, dict[str, list[str]]] = {}
    for tname, table in catalog.tables.items():
        for tag in table.tags:
            found.setdefault(tag, {"tables": [], "columns": []})["tables"].append(tname)
        for column in table.columns:
            for tag in column.tags:
                found.setdefault(tag, {"tables": [], "columns": []})["columns"].append(
                    f"{tname}.{column.name}"
                )
    return [
        {"tag": tag, "tables": sorted(where["tables"]), "columns": sorted(where["columns"])}
        for tag, where in sorted(found.items(), key=lambda item: item[0].lower())
    ]


def _retagged(tags: list[str], add: str | None = None, drop: str | None = None) -> list[str]:
    kept = [t for t in tags if drop is None or t.lower() != drop.lower()]
    if add is not None and all(t.lower() != add.lower() for t in kept):
        kept.append(add)
    return kept


def tag_targets(
    catalog: DatabaseCatalog,
    tag: str,
    targets: Iterable[tuple[str, str | None]],
    remove: bool = False,
) -> int:
    """Put ``tag`` on (or take it off) each ``(table, column or None)``; returns how many changed.

    Through the same edits a reviewer makes one by one, so each is kept as an
    override and its table marked edited, and a tag that makes a column look
    personal drops what analysis kept of its values.

    Raises:
        TableNotFoundInCatalogError, ColumnNotFoundInCatalogError: A target
            that is not in the catalog; nothing is changed then.
    """
    wanted = list(dict.fromkeys(targets))
    for table_name, column_name in wanted:  # all checked before any is changed
        table = _require_table(catalog, table_name)
        if column_name is not None and table.get_column(column_name) is None:
            raise ColumnNotFoundInCatalogError(column_name, table_name, catalog.database_name)
    changed = 0
    for table_name, column_name in wanted:
        current = _tags_at(catalog, table_name, column_name)
        updated = _retagged(current, drop=tag) if remove else _retagged(current, add=tag)
        if updated == current:
            continue
        if column_name is None:
            update_table_fields(catalog, table_name, {"tags": updated})
        else:
            update_column_fields(catalog, table_name, column_name, {"tags": updated})
        changed += 1
    return changed


def rename_tag(catalog: DatabaseCatalog, old: str, new: str) -> int:
    """Call ``old`` ``new`` wherever it is (merging into ``new`` if that exists); returns how many."""
    carriers = [
        (tname, None) for tname, table in catalog.tables.items() if _has(table.tags, old)
    ] + [
        (tname, column.name)
        for tname, table in catalog.tables.items()
        for column in table.columns
        if _has(column.tags, old)
    ]
    for table_name, column_name in carriers:
        current = _tags_at(catalog, table_name, column_name)
        updated = (
            _retagged(current, add=new, drop=old)
            if old.lower() != new.lower()
            else [new if t.lower() == old.lower() else t for t in current]
        )
        if column_name is None:
            update_table_fields(catalog, table_name, {"tags": updated})
        else:
            update_column_fields(catalog, table_name, column_name, {"tags": updated})
    return len(carriers)


def remove_tag(catalog: DatabaseCatalog, tag: str) -> int:
    """Take ``tag`` off everything that carries it; returns how many."""
    carriers = [(t["tag"], t) for t in tags_of(catalog) if t["tag"].lower() == tag.lower()]
    targets: list[tuple[str, str | None]] = []
    for _, where in carriers:
        targets += [(name, None) for name in where["tables"]]
        for carrier in where["columns"]:
            table_name, _, column_name = carrier.partition(".")
            targets.append((table_name, column_name))
    return tag_targets(catalog, tag, targets, remove=True)


def _tags_at(catalog: DatabaseCatalog, table_name: str, column_name: str | None) -> list[str]:
    table = _require_table(catalog, table_name)
    if column_name is None:
        return table.tags
    column = table.get_column(column_name)
    if column is None:
        raise ColumnNotFoundInCatalogError(column_name, table_name, catalog.database_name)
    return column.tags


def _has(tags: list[str], tag: str) -> bool:
    return any(t.lower() == tag.lower() for t in tags)


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


def record_model_changes(
    catalog: DatabaseCatalog, previous: DatabaseCatalog, keep: Iterable[str] = ()
) -> None:
    """After re-analysis: what the model now writes differently, and who edited what.

    For each table analysed again, a field whose model text changed keeps the
    earlier text in `previous_generated`, so a reviewer sees what changed
    rather than a new paragraph. The edit marks travel with the edits, which
    re-analysis re-applies. Tables in ``keep`` were not analysed again.
    """
    kept = set(keep)
    for name, table in catalog.tables.items():
        old = previous.tables.get(name)
        if old is None or name in kept:
            continue
        table.previous_generated = {}
        _note(table, "description", old.generated_description, table.generated_description)
        _note(table, "human_name", old.generated_human_name, table.generated_human_name)
        table.edited = dict(old.edited)
        for column in table.columns:
            before = old.get_column(column.name)
            if before is None:
                continue
            column.previous_generated = {}
            _note(column, "description", before.generated_description, column.generated_description)
            was, now = before.generated_semantic_type, column.generated_semantic_type
            if was is not None and was != now:
                column.previous_generated["semantic_type"] = was
            column.edited = dict(before.edited)


def _note(entry: Any, field_name: str, was: Any, now: Any) -> None:
    if was is not None and now is not None and was.texts and was.texts != now.texts:
        entry.previous_generated[field_name] = dict(was.texts)
