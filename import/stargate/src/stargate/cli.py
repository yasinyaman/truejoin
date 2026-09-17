"""Command line interface."""

import json
import sys
from pathlib import Path

import click
import structlog
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.tree import Tree

from stargate import __version__
from stargate.adapters.mcp_wire import server_to_mcp
from stargate.converter import convert_to_mcp, parse_spec

# Logging configuration
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

console = Console()


@click.group()
@click.version_option(version=__version__, prog_name="stargate")
def main():
    """Stargate - Converts REST APIs to MCP servers.

    Supported formats:
    - OpenAPI/Swagger 3.x (JSON/YAML)
    - Postman Collection v2.1
    - Manual definition
    """
    pass


@main.command()
@click.option(
    "--input",
    "-i",
    "input_path",
    help="Input file path",
)
@click.option(
    "--url",
    "-u",
    help="API specification URL",
)
@click.option(
    "--format",
    "-f",
    type=click.Choice(["openapi", "postman", "manual"], case_sensitive=False),
    help="Input format (auto-detected)",
)
@click.option(
    "--output",
    "-o",
    "output_path",
    help="Output file or directory",
)
@click.option(
    "--base-url",
    "-b",
    help="API base URL (if not in specification)",
)
@click.option(
    "--include-deprecated",
    is_flag=True,
    help="Include deprecated endpoints",
)
@click.option(
    "--filter-tags",
    "-t",
    multiple=True,
    help="Only include endpoints with specified tags",
)
@click.option(
    "--json-output",
    is_flag=True,
    help="Output in JSON format",
)
def convert(
    input_path: str | None,
    url: str | None,
    format: str | None,
    output_path: str | None,
    base_url: str | None,
    include_deprecated: bool,
    filter_tags: tuple,
    json_output: bool,
):
    """Converts API specification to MCP server.

    Examples:

        # Convert from OpenAPI
        stargate convert -i openapi.json -o ./my-mcp-server

        # Convert from URL
        stargate convert -u https://petstore3.swagger.io/api/v3/openapi.json

        # From Postman Collection
        stargate convert -i collection.json -f postman -o ./mcp-server
    """
    # Source check
    if not input_path and not url:
        console.print("[red]Error:[/red] --input or --url must be specified")
        sys.exit(1)

    source = url if url else input_path

    try:
        with console.status("[bold green]Converting..."):
            result = convert_to_mcp(
                source=source,
                base_url=base_url,
                format=format,
                include_deprecated=include_deprecated,
                filter_tags=list(filter_tags) if filter_tags else None,
            )

        if json_output:
            # JSON output
            output = {
                "success": result.success,
                "server": server_to_mcp(result.server),
                "metadata": result.metadata,
                "warnings": result.warnings,
                "errors": result.errors,
            }
            if output_path:
                Path(output_path).write_text(json.dumps(output, indent=2, ensure_ascii=False))
                console.print(f"[green]Output saved:[/green] {output_path}")
            else:
                console.print_json(data=output)
        else:
            # Rich output
            _print_result(result)

            if output_path:
                _save_output(result, output_path)

    except FileNotFoundError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)
    except Exception as e:
        console.print(f"[red]Unexpected error:[/red] {e}")
        if console.is_terminal:
            console.print_exception()
        sys.exit(1)


@main.command()
@click.option(
    "--input",
    "-i",
    "input_path",
    help="Input file path",
)
@click.option(
    "--url",
    "-u",
    help="API specification URL",
)
@click.option(
    "--format",
    "-f",
    type=click.Choice(["openapi", "postman", "manual"], case_sensitive=False),
    help="Input format",
)
def info(
    input_path: str | None,
    url: str | None,
    format: str | None,
):
    """Shows information about API specification.

    Examples:

        stargate info -i openapi.json
        stargate info -u https://petstore3.swagger.io/api/v3/openapi.json
    """
    if not input_path and not url:
        console.print("[red]Error:[/red] --input or --url must be specified")
        sys.exit(1)

    source = url if url else input_path

    try:
        with console.status("[bold green]Analyzing..."):
            schema = parse_spec(source=source, format=format)

        # Show info
        console.print()
        desc = schema.description
        if len(desc) > 200:
            desc = f"{desc[:200]}..."
        console.print(
            Panel(
                f"[bold]{schema.title}[/bold]\nVersion: {schema.version}\n{desc}",
                title="API Info",
            )
        )

        # Statistics
        table = Table(title="Statistics")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="green")

        table.add_row("Total Endpoints", str(len(schema.endpoints)))
        for method in ["GET", "POST", "PUT", "DELETE", "PATCH"]:
            count = len([e for e in schema.endpoints if e.method.value == method])
            table.add_row(method, str(count))
        table.add_row("Schema Definitions", str(len(schema.schemas)))
        table.add_row("Security Schemes", str(len(schema.security_schemes)))

        console.print(table)

        # Tags
        if schema.tags:
            console.print("\n[bold]Tags:[/bold]")
            for tag in schema.tags:
                name = tag.get("name", "")
                tag_desc = str(tag.get("description", ""))
                if len(tag_desc) > 50:
                    tag_desc = f"{tag_desc[:50]}..."
                console.print(f"  - {name}: {tag_desc}")

        # Servers
        if schema.servers:
            console.print("\n[bold]Servers:[/bold]")
            for server in schema.servers:
                console.print(f"  - {server.url}")

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


@main.command()
@click.option(
    "--input",
    "-i",
    "input_path",
    required=True,
    help="MCP server definition (JSON)",
)
@click.option(
    "--transport",
    "-t",
    type=click.Choice(["stdio", "sse", "streamable-http"]),
    default="stdio",
    help="Transport type",
)
@click.option(
    "--auth-token",
    help="API authentication token",
)
def serve(
    input_path: str,
    transport: str,
    auth_token: str | None,
):
    """Runs MCP server.

    Examples:

        # With stdio transport
        stargate serve -i server.json

        # With HTTP transport
        stargate serve -i server.json -t streamable-http
    """
    try:
        # Load server definition
        path = Path(input_path)
        if not path.exists():
            console.print(f"[red]Error:[/red] File not found: {path}")
            sys.exit(1)

        data = json.loads(path.read_text())

        from stargate.domain.mcp import MCPResource, MCPServer, MCPTool
        from stargate.server.runtime import MCPServerRuntime

        # Create server
        tools = [MCPTool(**t) for t in data.get("tools", [])]
        resources = [MCPResource(**r) for r in data.get("resources", [])]

        server = MCPServer(
            name=data.get("name", "MCP Server"),
            version=data.get("version", "1.0.0"),
            description=data.get("description", ""),
            tools=tools,
            resources=resources,
            base_url=data.get("base_url", ""),
            x_llm_context=data.get("x_llm_context"),
        )

        # Create and run runtime
        runtime = MCPServerRuntime(server, auth_token=auth_token)

        console.print("[green]Starting MCP server...[/green]")
        console.print(f"  Transport: {transport}")
        console.print(f"  Tools: {len(tools)}")
        console.print(f"  Resources: {len(resources)}")

        runtime.run(transport=transport)

    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


@main.command()
def formats():
    """Lists supported formats."""
    console.print("\n[bold]Supported Input Formats:[/bold]\n")

    tree = Tree("[bold]Formats[/bold]")

    openapi = tree.add("[cyan]OpenAPI/Swagger[/cyan]")
    openapi.add("OpenAPI 3.0.x")
    openapi.add("OpenAPI 3.1.x")
    openapi.add("Swagger 2.0 (limited)")
    openapi.add("JSON and YAML support")

    postman = tree.add("[cyan]Postman Collection[/cyan]")
    postman.add("Collection v2.1")
    postman.add("Folder structure support")
    postman.add("Environment variables")
    postman.add("Request/Response examples")

    manual = tree.add("[cyan]Manual Definition[/cyan]")
    manual.add("Simple JSON format")
    manual.add("Programmatic API creation")

    console.print(tree)


def _print_result(result):
    """Shows conversion result."""
    console.print()

    # Header
    status = "[green]Success[/green]" if result.success else "[red]Failed[/red]"
    console.print(
        Panel(
            f"[bold]{result.server.name}[/bold] v{result.server.version}\nStatus: {status}",
            title="Conversion Result",
        )
    )

    # Tools table
    if result.server.tools:
        table = Table(title=f"MCP Tools ({len(result.server.tools)})")
        table.add_column("Name", style="cyan")
        table.add_column("Method", style="yellow")
        table.add_column("Path", style="green")
        table.add_column("Description", max_width=40)

        for tool in result.server.tools[:20]:  # First 20
            desc = tool.description[:40] + "..." if len(tool.description) > 40 else tool.description
            table.add_row(tool.name, tool.http_method, tool.http_path, desc)

        if len(result.server.tools) > 20:
            table.add_row("...", "...", "...", f"and {len(result.server.tools) - 20} more tools")

        console.print(table)

    # Resources table
    if result.server.resources:
        table = Table(title=f"MCP Resources ({len(result.server.resources)})")
        table.add_column("Name", style="cyan")
        table.add_column("URI", style="green")
        table.add_column("Type", style="yellow")

        for resource in result.server.resources[:10]:  # First 10
            table.add_row(resource.name, resource.uri, resource.mime_type)

        if len(result.server.resources) > 10:
            table.add_row("...", "...", f"and {len(result.server.resources) - 10} more resources")

        console.print(table)

    # Warnings
    if result.warnings:
        console.print(f"\n[yellow]Warnings ({len(result.warnings)}):[/yellow]")
        for warning in result.warnings[:5]:
            console.print(f"  - {warning}")
        if len(result.warnings) > 5:
            console.print(f"  ... and {len(result.warnings) - 5} more warnings")

    # Errors
    if result.errors:
        console.print(f"\n[red]Errors ({len(result.errors)}):[/red]")
        for error in result.errors:
            console.print(f"  - {error}")


def _save_output(result, output_path: str):
    """Saves result to file."""
    path = Path(output_path)

    # If directory, save as server.json inside
    if path.is_dir() or not path.suffix:
        path.mkdir(parents=True, exist_ok=True)
        output_file = path / "server.json"
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        output_file = path

    output = {
        "name": result.server.name,
        "version": result.server.version,
        "description": result.server.description,
        "base_url": result.server.base_url,
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                "inputSchema": t.input_schema,
                "http_method": t.http_method,
                "http_path": t.http_path,
                **({"x_llm_context": t.x_llm_context} if t.x_llm_context else {}),
            }
            for t in result.server.tools
        ],
        "resources": [
            {
                "name": r.name,
                "description": r.description,
                "uri": r.uri,
                "mimeType": r.mime_type,
                "http_path": r.http_path,
                "base_url": r.base_url or result.server.base_url,
                **({"schema_def": r.schema_def} if r.schema_def else {}),
            }
            for r in result.server.resources
        ],
    }

    # Include global x-llm-context if available
    if result.server.x_llm_context:
        output["x_llm_context"] = result.server.x_llm_context

    output_file.write_text(json.dumps(output, indent=2, ensure_ascii=False))
    console.print(f"\n[green]Output saved:[/green] {output_file}")


if __name__ == "__main__":
    main()
