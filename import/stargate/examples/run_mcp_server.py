#!/usr/bin/env python3
"""
Stargate MCP Server Runner

Runs an MCP server from a saved server.json file (as written by
``stargate convert -o`` or the example converters). This is a thin wrapper
around ``stargate.runtime.MCPServerRuntime`` and behaves exactly like
``stargate serve -i <server.json>``; it exists so Claude Desktop can point at
a plain Python file.

Usage:
    python run_mcp_server.py <server_json_path> [--auth-token TOKEN] [--transport stdio|sse|streamable-http]

Example:
    python run_mcp_server.py ./petstore/output/petstore_server.json
    python run_mcp_server.py ./github/output/github_server.json --auth-token ghp_xxx
"""

import argparse
import sys
from pathlib import Path

try:
    from stargate.bootstrap import default_repository
    from stargate.runtime import MCPServerRuntime
except ImportError:
    print("Error: stargate is not installed. From the repository root run:", file=sys.stderr)
    print("  pip install -e .", file=sys.stderr)
    sys.exit(1)


def main():
    """Starts MCP server."""
    parser = argparse.ArgumentParser(
        description="Stargate MCP Server Runner (same as `stargate serve`)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python run_mcp_server.py ./petstore/output/petstore_server.json
  python run_mcp_server.py ./github/output/github_server.json --auth-token ghp_xxx

Claude Desktop Configuration (claude_desktop_config.json):
{
  "mcpServers": {
    "my-api": {
      "command": "python",
      "args": ["/path/to/stargate/examples/run_mcp_server.py", "/path/to/server.json"],
      "env": {
        "PYTHONPATH": "/path/to/stargate/src"
      }
    }
  }
}
        """,
    )
    parser.add_argument("server_json", help="Server JSON file path")
    parser.add_argument("--auth-token", help="API authentication token (Bearer)")
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="MCP transport type (default: stdio)",
    )
    args = parser.parse_args()

    try:
        server = default_repository().load(Path(args.server_json))
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except ValueError as e:  # JSON syntax or schema errors
        print(f"Error: invalid server definition: {e}", file=sys.stderr)
        sys.exit(1)

    runtime = MCPServerRuntime(server, auth_token=args.auth_token)

    # Info message (to stderr, since stdout is used for MCP)
    print(f"Starting MCP Server: {server.name}", file=sys.stderr)
    print(f"  Base URL: {server.base_url}", file=sys.stderr)
    print(f"  Tools: {len(server.tools)}", file=sys.stderr)
    print(f"  Resources: {len(server.resources)}", file=sys.stderr)
    print(f"  Transport: {args.transport}", file=sys.stderr)
    if args.auth_token:
        print("  Auth: Bearer token configured", file=sys.stderr)
    print("", file=sys.stderr)

    runtime.run(transport=args.transport)


if __name__ == "__main__":
    main()
