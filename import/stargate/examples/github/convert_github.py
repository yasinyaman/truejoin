#!/usr/bin/env python3
"""Convert GitHub API to MCP server and save to JSON."""

import json
from pathlib import Path

from stargate.adapters.parsers.manual import create_manual_api
from stargate.generators.mcp_server import generate_server


def main():
    """Converts GitHub API endpoints from manual definition."""
    print("Converting GitHub API (manual definition)...")
    print("-" * 50)

    # Manual API definition
    schema = create_manual_api(
        title="GitHub API",
        base_url="https://api.github.com",
        version="2022-11-28",
        description="GitHub REST API - Selected endpoints",
        auth={"type": "bearer", "description": "GitHub Personal Access Token"},
        endpoints=[
            # Users
            {
                "path": "/user",
                "method": "GET",
                "name": "getAuthenticatedUser",
                "summary": "Get the authenticated user",
                "description": "Get information about the authenticated user",
                "auth": True,
                "tags": ["users"],
            },
            {
                "path": "/users/{username}",
                "method": "GET",
                "name": "getUser",
                "summary": "Get a user",
                "description": "Get public information about a user",
                "tags": ["users"],
            },
            # Repos
            {
                "path": "/user/repos",
                "method": "GET",
                "name": "listUserRepos",
                "summary": "List repositories for the authenticated user",
                "parameters": [
                    {"name": "type", "in": "query", "type": "string"},
                    {"name": "sort", "in": "query", "type": "string"},
                    {"name": "per_page", "in": "query", "type": "integer"},
                ],
                "auth": True,
                "tags": ["repos"],
            },
            {
                "path": "/repos/{owner}/{repo}",
                "method": "GET",
                "name": "getRepo",
                "summary": "Get a repository",
                "tags": ["repos"],
            },
            {
                "path": "/repos/{owner}/{repo}/issues",
                "method": "GET",
                "name": "listRepoIssues",
                "summary": "List repository issues",
                "parameters": [
                    {"name": "state", "in": "query", "type": "string"},
                    {"name": "labels", "in": "query", "type": "string"},
                    {"name": "per_page", "in": "query", "type": "integer"},
                ],
                "tags": ["issues"],
            },
            {
                "path": "/repos/{owner}/{repo}/issues",
                "method": "POST",
                "name": "createIssue",
                "summary": "Create an issue",
                "body": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "title": {"type": "string"},
                            "body": {"type": "string"},
                            "labels": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["title"],
                    }
                },
                "auth": True,
                "tags": ["issues"],
            },
            # Gists
            {
                "path": "/gists",
                "method": "GET",
                "name": "listGists",
                "summary": "List gists for the authenticated user",
                "auth": True,
                "tags": ["gists"],
            },
            {
                "path": "/gists",
                "method": "POST",
                "name": "createGist",
                "summary": "Create a gist",
                "body": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "description": {"type": "string"},
                            "public": {"type": "boolean"},
                            "files": {"type": "object"},
                        },
                        "required": ["files"],
                    }
                },
                "auth": True,
                "tags": ["gists"],
            },
        ],
    )

    # Create MCP Server
    result = generate_server(schema)

    if result.success:
        print(f"✓ Conversion successful!")
        print(f"  - API: {result.server.name} v{result.server.version}")
        print(f"  - Tools: {len(result.server.tools)}")
        print(f"  - Resources: {len(result.server.resources)}")
        print()

        # List tools
        print("MCP Tools:")
        for tool in result.server.tools:
            auth_marker = "🔒" if tool.security else "🔓"
            print(f"  {auth_marker} {tool.name}: {tool.http_method} {tool.http_path}")

        # Save JSON output
        output_dir = Path(__file__).parent / "output"
        output_dir.mkdir(exist_ok=True)

        server_file = output_dir / "github_server.json"
        server_data = {
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
                    "security": t.security,
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
                }
                for r in result.server.resources
            ],
        }
        server_file.write_text(json.dumps(server_data, indent=2, ensure_ascii=False))
        print(f"\n✓ Server JSON saved: {server_file}")

    else:
        print(f"✗ Conversion failed!")
        for error in result.errors:
            print(f"  - {error}")


if __name__ == "__main__":
    main()
