"""Warp Catalog CLI - Database Catalog Intelligence.

Commands:
    analyze        - Analyze database and generate catalog
    review         - Interactive table-by-table review of draft catalog
    approve        - Approve a catalog without the interactive walk
    suggest        - Suggest draft measures from the query history and the views
    certify        - Certify suggested expressions, so they become measures
    import         - Import a catalog, or an Ossie / MetricFlow model, as drafts
    export         - Export catalog to JSON/YAML/Markdown
    list           - List available catalogs
    info           - Show catalog info
    enrich-openapi - Enrich OpenAPI spec with catalog
    pipeline       - Run full pipeline (DB -> Catalog -> Enriched MCP)
"""

import asyncio
import getpass
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from truejoin import __version__
from truejoin.application.container import Container
from truejoin.domain.governance.audit import AuditAction, AuditEvent

if TYPE_CHECKING:
    from truejoin.application.catalog.catalog_review import CatalogReviewService
    from truejoin.application.catalog.history_mining import MiningReport
    from truejoin.domain.catalog.catalog import DatabaseCatalog, TableCatalogEntry

ContainerFactory = Callable[[str], Container]
Recorder = Callable[[AuditAction, str, str, str], None]


def _container(ctx: click.Context) -> Container:
    """Build the container for the config path given to the group.

    The factory is injected through the group's `context_settings["obj"]`
    (see `cli.main`), so this adapter never imports the composition root.
    """
    factory: ContainerFactory | None = ctx.obj.get("container_factory")
    if factory is None:
        raise click.UsageError("No container factory configured; run through `truejoin catalog`.")
    return factory(ctx.obj["config_path"])


def _recorder(container: Container) -> Recorder:
    """Audit a catalog change made here, as the HTTP routes do (names only, never text)."""
    try:
        actor = f"cli:{getpass.getuser()}"
    except Exception:  # no login name in this environment (getpass raises OSError or KeyError)
        actor = "cli"

    def record(action: AuditAction, database: str, table: str, change: str) -> None:
        container.audit.record(
            AuditEvent(action=action, database=database, table=table, actor=actor, change=change)
        )

    return record


@click.group()
@click.version_option(version=__version__)
@click.option(
    "-c",
    "--config",
    default="config/database.yaml",
    help="Config file path",
    type=click.Path(),
)
@click.pass_context
def main(ctx: click.Context, config: str) -> None:
    """Warp Catalog - Database Catalog Intelligence.

    LLM-powered database schema analysis and description generation.
    """
    # Copy the (shared) default obj from context_settings before adding per-run state.
    ctx.obj = {**(ctx.obj or {}), "config_path": config}


@main.command()
@click.option("-d", "--database", required=True, help="Database config name")
@click.option("-t", "--tables", default=None, help="Comma-separated table names")
@click.option("--lang", default=None, help="Language override")
@click.option("-f", "--format", "fmt", default="json", help="Export format: json, yaml, markdown")
@click.option("-o", "--output", default=None, help="Output file path")
@click.option("--auto-approve", is_flag=True, help="Skip review, approve directly")
@click.pass_context
def analyze(
    ctx: click.Context,
    database: str,
    tables: str | None,
    lang: str | None,
    fmt: str,
    output: str | None,
    auto_approve: bool,
) -> None:
    """Analyze database and generate catalog."""
    asyncio.run(_run_analyze(ctx, database, tables, lang, fmt, output, auto_approve))


async def _run_analyze(  # noqa: PLR0913
    ctx: click.Context,
    database: str,
    tables: str | None,
    lang: str | None,
    fmt: str,
    output: str | None,
    auto_approve: bool = False,
) -> None:
    from truejoin.domain.gateway.errors import DatabaseNotConfiguredError

    container = _container(ctx)
    config = container.settings

    lang = lang or config.settings.i18n.default_language
    table_names = tables.split(",") if tables else None

    try:
        async with container.open_analysis(database) as analysis:
            catalog = await analysis.analyze(table_names=table_names, auto_approve=auto_approve)
            report = analysis.report
    except DatabaseNotConfiguredError:
        click.echo(f"Error: Database config '{database}' not found", err=True)
        sys.exit(1)
    change = "analyze" + (" auto_approve" if auto_approve else "")
    _recorder(container)("create", catalog.database_name, ",".join(table_names or ()), change)

    click.echo(f"Catalog generated: {catalog.table_count} tables, languages: {catalog.languages}")
    if report and report.fallback_tables:
        click.echo(
            f"Warning: {len(report.fallback_tables)} table(s) have no LLM descriptions "
            f"(schema only): {', '.join(report.fallback_tables)}",
            err=True,
        )
    if report and report.failed_tables:
        click.echo(
            f"Warning: {len(report.failed_tables)} table(s) could not be read and are missing "
            f"from the catalog: {', '.join(report.failed_tables)}",
            err=True,
        )

    if auto_approve:
        click.echo("Status: APPROVED (auto-approved)")
    else:
        click.echo(
            f"Status: DRAFT - Run 'truejoin catalog review -d {database}' to review and approve."
        )

    if output:
        path = container.export.export(catalog, fmt, output, lang=lang)
        click.echo(f"Exported to: {path}")


@main.command("import")
@click.option("-d", "--database", required=True, help="Database/catalog name")
@click.option(
    "-f",
    "--format",
    "fmt",
    type=click.Choice(["truejoin", "ossie", "metricflow"]),
    default="truejoin",
    help="truejoin: a catalog exported with lang=all; ossie or metricflow: a semantic model",
)
@click.option("--replace", is_flag=True, help="Replace the catalog there is")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
@click.pass_context
def import_catalog(ctx: click.Context, database: str, fmt: str, replace: bool, path: str) -> None:
    """Import a catalog, or a semantic model from another tool, as drafts to review."""
    asyncio.run(
        _run_import(ctx, database, fmt, replace, Path(path).read_text(encoding="utf-8-sig"))
    )


async def _run_import(
    ctx: click.Context, database: str, fmt: str, replace: bool, text: str
) -> None:
    import yaml

    from truejoin.application.gateway.schema_discovery import SchemaAnalyzer
    from truejoin.domain.catalog.catalog import DatabaseCatalog, structural_catalog
    from truejoin.domain.gateway.errors import DatabaseNotConfiguredError

    container = _container(ctx)
    existing = container.repository.load(database)
    change = f"import{'' if fmt == 'truejoin' else ' ' + fmt}" + (" replace" if replace else "")
    if fmt == "truejoin":
        if existing is not None and not replace:
            click.echo(f"{database} has a catalog; use --replace to replace it", err=True)
            sys.exit(1)
        document = yaml.safe_load(text)
        if not isinstance(document, dict) or "tables" not in document:
            click.echo(
                "Not a Truejoin catalog (export it with --lang all), or use -f ossie|metricflow",
                err=True,
            )
            sys.exit(1)
        imported = DatabaseCatalog.model_validate({**document, "database_name": database})
        container.review.import_catalog(imported)
        _recorder(container)("create", database, "", change)
        click.echo(f"Imported {imported.table_count} tables as a draft: review and approve them.")
        return
    try:
        draft = container.semantic_readers[fmt].read(text)
    except (ValueError, yaml.YAMLError) as e:
        click.echo(f"Error: {e}", err=True)
        sys.exit(1)
    catalog = None if replace else existing
    if catalog is None:
        try:
            db_config = container.database(database)
        except DatabaseNotConfiguredError:
            click.echo(f"Error: {database} has no catalog and no database config to read", err=True)
            sys.exit(1)
        async with container.open_gateway(db_config) as gateway:
            schema = await SchemaAnalyzer(gateway).analyze()
        catalog = structural_catalog(database, schema, db_config.type)
    if container.expressions is None:  # pragma: no cover - every build has one
        click.echo("Error: this build cannot check measures", err=True)
        sys.exit(1)
    lang = catalog.languages[0] if catalog.languages else "en"
    report = container.review.import_semantic_model(
        catalog, draft, container.expressions, lang=lang
    )
    _recorder(container)("create", database, "", change)
    click.echo(
        f"Imported from {fmt}: {len(report.tables)} table(s) touched, "
        f"{report.descriptions} description(s), {report.words} word(s), "
        f"{report.relationships} relationship(s), {len(report.measures)} measure(s), as drafts."
    )
    for item in report.skipped:
        click.echo(f"  skipped {item.item}: {item.reason}")
    click.echo(f"Review with 'truejoin catalog review -d {database}', then certify the measures.")


@main.command("export")
@click.option("-d", "--database", required=True, help="Database/catalog name")
@click.option("-f", "--format", "fmt", default="json", help="Export format")
@click.option("-o", "--output", required=True, help="Output file path")
@click.option("--lang", default="en", help="Language for export")
@click.pass_context
def export_catalog(ctx: click.Context, database: str, fmt: str, output: str, lang: str) -> None:
    """Export catalog to a file."""
    from truejoin.domain.gateway.errors import UnsupportedExportFormatError

    container = _container(ctx)
    catalog = container.repository.load(database)
    if not catalog:
        click.echo(f"Catalog not found: {database}", err=True)
        sys.exit(1)

    try:
        path = container.export.export(catalog, fmt, output, lang=lang)
    except UnsupportedExportFormatError as e:
        click.echo(f"Error: {e.message}", err=True)
        sys.exit(1)
    click.echo(f"Exported {catalog.table_count} tables to: {path}")


@main.command("list")
@click.pass_context
def list_catalogs(ctx: click.Context) -> None:
    """List available catalogs."""
    store = _container(ctx).repository

    catalogs = store.list_catalogs()
    if not catalogs:
        click.echo("No catalogs found.")
        return

    index = store.get_index()
    for name in catalogs:
        entry = index.catalogs.get(name)
        if entry:
            status = getattr(entry, "status", "draft")
            click.echo(
                f"  {name}: {entry.table_count} tables, "
                f"{entry.database_type}, "
                f"langs={entry.languages}, "
                f"v{entry.version}, "
                f"status={status}"
            )
        else:
            click.echo(f"  {name}")


@main.command("info")
@click.option("-d", "--database", required=True, help="Database/catalog name")
@click.option("--lang", default="en", help="Language for descriptions")
@click.pass_context
def show_info(ctx: click.Context, database: str, lang: str) -> None:
    """Show catalog info."""
    catalog = _container(ctx).repository.load(database)
    if not catalog:
        click.echo(f"Catalog not found: {database}", err=True)
        sys.exit(1)

    click.echo(f"Database: {catalog.database_name}")
    click.echo(f"Type: {catalog.database_type}")
    click.echo(f"Status: {catalog.status.value}")
    click.echo(f"Tables: {catalog.table_count}")
    click.echo(f"Languages: {catalog.languages}")
    click.echo(f"Generated: {catalog.generated_at}")
    click.echo(f"LLM: {catalog.llm_provider}/{catalog.llm_model}")

    desc = catalog.description.get(lang)
    if desc:
        click.echo(f"Description: {desc}")

    click.echo("\nTables:")
    for tname, table in catalog.tables.items():
        human = table.human_name.get(lang) or tname
        tdesc = table.description.get(lang, "")
        rows = f" (~{table.row_count} rows)" if table.row_count else ""
        click.echo(f"  {human} ({tname}): {len(table.columns)} cols{rows}")
        if tdesc:
            click.echo(f"    {tdesc}")


@main.command("review")
@click.option("-d", "--database", required=True, help="Database/catalog name to review")
@click.option("--lang", default="en", help="Language for display and editing")
@click.option("--auto-approve", is_flag=True, help="Approve all without prompting")
@click.pass_context
def review_catalog(ctx: click.Context, database: str, lang: str, auto_approve: bool) -> None:  # noqa: C901, PLR0912, PLR0915
    """Interactive table-by-table review of a draft catalog.

    Review LLM-generated descriptions and edit any field before approving.
    """
    from truejoin.domain.catalog.catalog import CatalogStatus

    container = _container(ctx)
    store = container.review
    record = _recorder(container)

    catalog = store.load(database)
    if not catalog:
        click.echo(f"Catalog not found: {database}", err=True)
        sys.exit(1)

    if catalog.status == CatalogStatus.approved:
        click.echo(f"Catalog '{database}' is already approved.")
        if not click.confirm("Re-open for review?"):
            return
        store.save_as_draft(catalog)
        record("update", database, "", "reopen")

    click.echo(f"\n{'=' * 60}")
    click.echo(f" Review: {catalog.database_name} ({catalog.table_count} tables)")
    click.echo(f" Status: {catalog.status.value}")
    click.echo(f" Languages: {catalog.languages}")
    click.echo(f"{'=' * 60}\n")

    if auto_approve:
        store.approve_catalog(database)
        record("update", database, "", "approve_catalog")
        click.echo("All tables auto-approved. Catalog status: approved.")
        return

    table_names = list(catalog.tables.keys())
    for i, tname in enumerate(table_names, 1):
        table = catalog.tables[tname]

        override_marker = " [user-edited]" if table.user_overrides else ""
        click.echo(f"\n--- Table {i}/{len(table_names)}: {tname}{override_marker} ---")
        click.echo(f"  Human name:  {table.human_name.get(lang) or '(empty)'}")
        click.echo(f"  Description: {table.description.get(lang) or '(empty)'}")
        click.echo(f"  Tags:        {', '.join(table.tags) if table.tags else '(none)'}")
        click.echo(f"  Rows:        {table.row_count or 'N/A'}")
        click.echo(f"  Columns ({len(table.columns)}):")

        for col in table.columns:
            pk_marker = " [PK]" if col.is_primary_key else ""
            fk_marker = f" [FK->{col.references}]" if col.is_foreign_key else ""
            sem = f" ({col.semantic_type})" if col.semantic_type else ""
            desc = col.description.get(lang) or ""
            click.echo(f"    {col.name} ({col.data_type}){pk_marker}{fk_marker}{sem}")
            if desc:
                click.echo(f"      {desc}")
            if col.tags:
                click.echo(f"      tags: {', '.join(col.tags)}")

        if table.relationships:
            click.echo(f"  Relationships ({len(table.relationships)}):")
            for rel in table.relationships:
                click.echo(
                    f"    {rel.source_column} -> {rel.target_table}.{rel.target_column}"
                    f" ({rel.relationship_type})"
                    + (" [contradicted by the data]" if rel.verified is False else "")
                )
                if not rel.description.is_empty:
                    click.echo(f"      {rel.description.get(lang)}")

        _show_terms(catalog, tname)
        click.echo()

        while True:
            action = click.prompt(
                "  Action: [a]pprove, [e]dit, [s]kip, [q]uit",
                type=click.Choice(["a", "e", "s", "q"], case_sensitive=False),
                default="a",
            )

            if action == "a":
                # The walk has just shown this table's term.
                store.approve_table(database, tname, certify_term=True)
                record("update", database, tname, "approve_table certify_term")
                click.echo(f"  -> {tname} approved.")
                break
            elif action == "s":
                click.echo(f"  -> {tname} skipped (remains pending).")
                break
            elif action == "q":
                click.echo("\nReview paused. Run 'truejoin catalog review' to continue.")
                return
            elif action == "e":
                _interactive_edit_table(store, record, database, tname, table, lang)
                # Reload table after edits
                catalog = store.load_or_raise(database)
                table = catalog.tables[tname]
                click.echo(f"\n  Updated {tname}:")
                click.echo(f"    Human name:  {table.human_name.get(lang)}")
                click.echo(f"    Description: {table.description.get(lang)}")
                click.echo(f"    Tags:        {', '.join(table.tags) if table.tags else '(none)'}")
                _show_terms(catalog, tname)
                continue  # Re-prompt action

    # Final check
    catalog = store.load_or_raise(database)
    summary = catalog.review_summary
    click.echo(f"\nReview summary: {summary}")

    pending_count = summary.get("pending", 0)
    if pending_count > 0:
        if click.confirm(f"{pending_count} table(s) still pending. Approve all remaining?"):
            store.approve_catalog(database)
            record("update", database, "", "approve_catalog")
            click.echo("Catalog approved.")
        else:
            click.echo("Catalog remains in draft status.")
    else:
        store.approve_catalog(database)
        record("update", database, "", "approve_catalog")
        click.echo("All tables reviewed. Catalog approved!")


@main.command("approve")
@click.option("-d", "--database", required=True, help="Database/catalog name to approve")
@click.option(
    "--certify-terms",
    is_flag=True,
    help="Also certify every business term, unseen (review certifies them one table at a time)",
)
@click.pass_context
def approve_catalog(ctx: click.Context, database: str, certify_terms: bool) -> None:
    """Approve every table of a catalog (what `review --auto-approve` does, unprompted)."""
    container = _container(ctx)
    store = container.review
    record = _recorder(container)
    if not store.load(database):
        click.echo(f"Catalog not found: {database}", err=True)
        sys.exit(1)
    catalog = store.approve_catalog(database)
    record("update", database, "", "approve_catalog")
    click.echo(f"Catalog '{database}' approved.")
    if certify_terms:
        certified = store.certify_terms(database)
        record("update", database, "", f"certify_terms {certified}")
        click.echo(f"{certified} term(s) certified.")
    else:
        waiting = sum(1 for term in catalog.terms.values() if not term.certified)
        if waiting:
            click.echo(
                f"{waiting} term(s) are not certified and will not be used: "
                f"`catalog review -d {database}` shows them, --certify-terms accepts them all."
            )


@main.command("suggest")
@click.option("-d", "--database", required=True, help="Database config name")
@click.option("--limit", default=20, show_default=True, help="Most drafts to add in one run")
@click.option(
    "--statements", default=500, show_default=True, help="Most statements to read from the history"
)
@click.option("--no-history", is_flag=True, help="Read the views only")
@click.option("--no-views", is_flag=True, help="Read the statement history only")
@click.pass_context
def suggest(  # noqa: PLR0913
    ctx: click.Context,
    database: str,
    limit: int,
    statements: int,
    no_history: bool,
    no_views: bool,
) -> None:
    """Suggest measures from the expressions the database's own queries repeat.

    Reads the statement history and the view definitions, keeps the scalar
    expressions that recur, has the model name them and stores them in the
    catalog as drafts. A draft is not a measure until `catalog certify`.
    """
    container = _container(ctx)
    if not container.review.load(database):
        click.echo(f"Catalog not found: {database}. Run `catalog analyze` first.", err=True)
        sys.exit(1)

    async def run() -> "MiningReport":
        async with container.open_mining(database) as mining:
            return await mining.suggest(
                limit=limit,
                statements=statements,
                use_history=not no_history,
                use_views=not no_views,
            )

    try:
        report = asyncio.run(run())
    except Exception as e:
        click.echo(f"Could not mine {database}: {e}", err=True)
        sys.exit(1)

    for reason in report.unavailable:
        click.echo(f"Not read: {reason}", err=True)
    click.echo(f"Read {report.statements} statement(s) and {report.views} view(s).")
    for table in report.without_term:
        click.echo(
            f"  {table}: has expressions but no term to hold them (analyze it with olap.terms on)."
        )
    if not report.added:
        click.echo("Nothing new to suggest.")
        if report.unavailable and not report.statements and not report.views:
            sys.exit(1)
        return
    _recorder(container)(
        "update",
        database,
        ",".join(sorted({draft.table for draft in report.added})),
        f"suggest_sql {','.join(draft.name for draft in report.added)}",
    )
    click.echo(f"{len(report.added)} draft(s) added to the catalog:")
    for draft in report.added:
        click.echo(f"  {draft.table}.{draft.name} = {draft.expression}")
        if not draft.description.is_empty:
            click.echo(f"      {draft.description}")
    names = " ".join(f"-e {draft.name}" for draft in report.added[:3])
    click.echo(
        f"None is a measure yet. Certify the ones that are right:\n"
        f"  catalog certify -d {database} {names}"
    )


@main.command("certify")
@click.option("-d", "--database", required=True, help="Database/catalog name")
@click.option(
    "-e", "--expression", "names", multiple=True, required=True, help="Expression name (repeatable)"
)
@click.pass_context
def certify(ctx: click.Context, database: str, names: tuple[str, ...]) -> None:
    """Certify draft expressions by name: they become measures.

    A running server picks the change up within 30 seconds; nothing needs a
    restart.

    The catalog's review status is left as it is; nothing else has to be
    reviewed again.
    """
    container = _container(ctx)
    store = container.review
    if not store.load(database):
        click.echo(f"Catalog not found: {database}", err=True)
        sys.exit(1)
    try:
        certified = store.certify_expressions(database, names)
    except KeyError as e:
        click.echo(str(e.args[0]), err=True)
        sys.exit(1)
    _recorder(container)(
        "update",
        database,
        ",".join(sorted({expression.table for expression in certified})),
        f"certify_sql={','.join(expression.name for expression in certified)}",
    )
    for expression in certified:
        how = "" if expression.aggregation == "sum" else f" [{expression.aggregation}]"
        click.echo(f"Certified {expression.table}.{expression.name} = {expression.expression}{how}")


def _show_terms(catalog: "DatabaseCatalog", table_name: str) -> None:
    """Print the business terms that cover a table, as the reviewer will certify them."""
    for term in catalog.terms.values():
        if table_name not in term.tables:
            continue
        state = "certified" if term.certified else "not certified"
        click.echo(f"  Term: {term.name} [{state}]")
        click.echo(f"    Also called: {', '.join(term.synonyms) or '(nothing)'}")
        for attribute in term.column_attributes:
            if attribute.table != table_name:
                continue
            values = "; ".join(
                f"{value} = {', '.join(words)}"
                for value, words in attribute.values.items()
                if words
            )
            if attribute.synonyms or values:
                click.echo(
                    f"    {attribute.column}: {', '.join(attribute.synonyms) or '-'}"
                    + (f"  [{values}]" if values else "")
                )
        for expression in term.sql_attributes:
            mark = "certified" if expression.certified else "not certified"
            how = "" if expression.aggregation == "sum" else f" [{expression.aggregation}]"
            click.echo(f"    Expression {expression.name} [{mark}]: {expression.expression}{how}")


def _interactive_edit_table(  # noqa: PLR0913
    store: "CatalogReviewService",
    record: Recorder,
    db_name: str,
    table_name: str,
    table: "TableCatalogEntry",
    lang: str,
) -> None:
    """Interactive edit sub-menu for a single table."""
    while True:
        click.echo("\n  Edit fields:")
        click.echo("    1. Description")
        click.echo("    2. Human name")
        click.echo("    3. Tags")
        click.echo("    4. Column description")
        click.echo("    5. Column semantic_type")
        click.echo("    6. Column tags")
        click.echo("    7. Done editing")
        click.echo("    8. Term name")
        click.echo("    9. Term synonyms")
        click.echo("    10. Words for a column (term)")
        click.echo("    11. Certify an expression (term)")

        choice = click.prompt("  Choose", type=int, default=7)

        if choice == 1:
            new_val = click.prompt(
                f"  New description ({lang})",
                default=table.description.get(lang),
            )
            store.update_table_fields(db_name, table_name, {"description": new_val}, lang)
            record("update", db_name, table_name, "edit_table description")
            click.echo("  -> Description updated.")

        elif choice == 2:
            new_val = click.prompt(
                f"  New human name ({lang})",
                default=table.human_name.get(lang),
            )
            store.update_table_fields(db_name, table_name, {"human_name": new_val}, lang)
            record("update", db_name, table_name, "edit_table human_name")
            click.echo("  -> Human name updated.")

        elif choice == 3:
            current = ", ".join(table.tags) if table.tags else ""
            new_val = click.prompt(
                "  Tags (comma-separated)",
                default=current,
            )
            tags = [t.strip() for t in new_val.split(",") if t.strip()]
            store.update_table_fields(db_name, table_name, {"tags": tags}, lang)
            record("update", db_name, table_name, "edit_table tags")
            click.echo("  -> Tags updated.")

        elif choice in (4, 5, 6):
            col_names = [c.name for c in table.columns]
            click.echo(f"  Columns: {', '.join(col_names)}")
            col_name = click.prompt("  Column name")
            col = table.get_column(col_name)
            if not col:
                click.echo(f"  Column '{col_name}' not found.")
                continue

            if choice == 4:
                new_val = click.prompt(
                    f"  New description ({lang})",
                    default=col.description.get(lang),
                )
                store.update_column_fields(
                    db_name,
                    table_name,
                    col_name,
                    {"description": new_val},
                    lang,
                )
                record("update", db_name, table_name, f"edit_column {col_name} description")
                click.echo("  -> Column description updated.")

            elif choice == 5:
                new_val = click.prompt(
                    "  New semantic_type",
                    default=col.semantic_type or "",
                )
                store.update_column_fields(
                    db_name,
                    table_name,
                    col_name,
                    {"semantic_type": new_val if new_val else None},
                    lang,
                )
                record("update", db_name, table_name, f"edit_column {col_name} semantic_type")
                click.echo("  -> Semantic type updated.")

            elif choice == 6:
                current = ", ".join(col.tags) if col.tags else ""
                new_val = click.prompt(
                    "  Tags (comma-separated)",
                    default=current,
                )
                tags = [t.strip() for t in new_val.split(",") if t.strip()]
                store.update_column_fields(
                    db_name,
                    table_name,
                    col_name,
                    {"tags": tags},
                    lang,
                )
                record("update", db_name, table_name, f"edit_column {col_name} tags")
                click.echo("  -> Column tags updated.")

        elif choice == 7:
            break
        elif choice in (8, 9, 10, 11):
            _edit_term(store, record, db_name, table_name, choice)
        else:
            click.echo("  Invalid choice.")

        # Reload to reflect changes
        catalog = store.load_or_raise(db_name)
        table = catalog.tables[table_name]


def _edit_term(
    store: "CatalogReviewService", record: Recorder, db_name: str, table_name: str, choice: int
) -> None:
    """One edit to the business term that covers a table."""
    catalog = store.load_or_raise(db_name)
    term = next((t for t in catalog.terms.values() if table_name in t.tables), None)
    if term is None:
        click.echo("  This table has no term (olap.terms was off when it was analysed).")
        return
    updates: dict[str, Any]
    if choice == 8:
        updates = {"name": click.prompt("  Term name", default=term.name)}
    elif choice == 9:
        words = click.prompt("  Synonyms (comma-separated)", default=", ".join(term.synonyms))
        updates = {"synonyms": [w.strip() for w in words.split(",")]}
    elif choice == 10:
        column = click.prompt("  Column name")
        current = term.attribute(table_name, column)
        words = click.prompt(
            "  Words (comma-separated)", default=", ".join(current.synonyms) if current else ""
        )
        updates = {"columns": {column: [w.strip() for w in words.split(",")]}}
    else:
        updates = {"certify_sql": [click.prompt("  Expression name")]}
    try:
        updated = store.update_term(db_name, term.name, updates)
    except KeyError as e:
        click.echo(f"  {e.args[0]}")
    else:
        # Field and measure names only, as the HTTP route records them.
        change = f"edit_term {term.name} {','.join(sorted(updates))}"
        if "certify_sql" in updates:
            change += f" certify_sql={','.join(updates['certify_sql'])}"
        record("update", db_name, ",".join(updated.tables), change)
        click.echo("  -> Term updated.")


@main.command("enrich-openapi")
@click.option("-d", "--database", required=True, help="Catalog name")
@click.option("-i", "--input", "input_path", required=True, help="OpenAPI spec file")
@click.option("-o", "--output", default=None, help="Output file path")
@click.option("--lang", default="en", help="Language for descriptions")
@click.option(
    "--include-examples",
    is_flag=True,
    help="Write real sample values into the spec (off by default: may contain PII)",
)
@click.pass_context
def enrich_openapi(  # noqa: PLR0913
    ctx: click.Context,
    database: str,
    input_path: str,
    output: str | None,
    lang: str,
    include_examples: bool,
) -> None:
    """Enrich OpenAPI spec with catalog descriptions."""
    from truejoin.application.catalog.openapi_enrichment import OpenAPIEnricher, withheld_from_spec

    container = _container(ctx)
    catalog = container.repository.load(database)
    if not catalog:
        click.echo(f"Catalog not found: {database}", err=True)
        sys.exit(1)

    enricher = OpenAPIEnricher(
        catalog,
        lang=lang,
        include_examples=include_examples,
        **withheld_from_spec(container.settings.settings),
    )
    result_path = enricher.enrich_file(input_path, output)
    click.echo(f"Enriched spec saved to: {result_path}")


@main.command("pipeline")
@click.option("-d", "--database", required=True, help="Database config name")
@click.option("--lang", default=None, help="Language")
@click.option("-t", "--tables", default=None, help="Comma-separated table names")
@click.option("-f", "--format", "fmt", default=None, help="Export format")
@click.option("-o", "--output", default=None, help="Export output path")
@click.option("--openapi", default=None, help="OpenAPI spec to enrich")
@click.pass_context
def run_pipeline(
    ctx: click.Context,
    database: str,
    lang: str | None,
    tables: str | None,
    fmt: str | None,
    output: str | None,
    openapi: str | None,
) -> None:
    """Run full pipeline: DB -> Catalog -> Enriched MCP."""
    asyncio.run(_run_pipeline(ctx, database, lang, tables, fmt, output, openapi))


async def _run_pipeline(
    ctx: click.Context,
    database: str,
    lang: str | None,
    tables: str | None,
    fmt: str | None,
    output: str | None,
    openapi: str | None,
) -> None:
    table_names = tables.split(",") if tables else None

    container = _container(ctx)
    result = await container.pipeline().run(
        database_name=database,
        lang=lang,
        table_names=table_names,
        export_format=fmt,
        export_path=output,
        openapi_spec_path=openapi,
    )
    _recorder(container)("create", database, ",".join(table_names or ()), "analyze")

    click.echo(f"Pipeline completed: {result.catalog.table_count} tables analyzed")
    if result.export_path:
        click.echo(f"Exported to: {result.export_path}")
    if result.enriched_openapi_path:
        click.echo(f"Enriched OpenAPI: {result.enriched_openapi_path}")


if __name__ == "__main__":
    main()
