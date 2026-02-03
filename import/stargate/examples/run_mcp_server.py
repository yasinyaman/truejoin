#!/usr/bin/env python3
"""
Stargate MCP Server Runner

This script runs an MCP server from a saved server.json file.
Can be used with Claude Desktop or other MCP clients.

Usage:
    python run_mcp_server.py <server_json_path> [--auth-token TOKEN]

Example:
    python run_mcp_server.py ./petstore/output/petstore_server.json
    python run_mcp_server.py ./github/output/github_server.json --auth-token ghp_xxx
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

try:
    from mcp.server.fastmcp import FastMCP
    import httpx
except ImportError:
    print("Error: MCP SDK not installed. To install:")
    print("  pip install mcp httpx")
    sys.exit(1)


def load_server_config(config_path: str) -> dict:
    """Loads server JSON file."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Server file not found: {path}")
    
    return json.loads(path.read_text(encoding="utf-8"))


def create_tool_handler(tool_config: dict, base_url: str, auth_token: str | None):
    """Creates async handler for tool."""
    
    async def handler(**kwargs):
        """Calls API endpoint."""
        # Build URL
        url = f"{base_url}{tool_config['http_path']}"
        
        # Apply path parameters
        path_params = {}
        query_params = {}
        body_params = {}
        
        # Separate parameters
        input_schema = tool_config.get("inputSchema", {})
        properties = input_schema.get("properties", {})
        
        for key, value in kwargs.items():
            if value is None:
                continue
            
            # Path parameter if in URL
            if f"{{{key}}}" in url:
                path_params[key] = value
                url = url.replace(f"{{{key}}}", str(value))
            elif tool_config["http_method"] in ("POST", "PUT", "PATCH"):
                body_params[key] = value
            else:
                query_params[key] = value
        
        # Headers
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        
        # HTTP request
        async with httpx.AsyncClient(timeout=30.0) as client:
            method = tool_config["http_method"]
            
            try:
                if method == "GET":
                    response = await client.get(url, params=query_params, headers=headers)
                elif method == "POST":
                    response = await client.post(url, params=query_params, json=body_params or None, headers=headers)
                elif method == "PUT":
                    response = await client.put(url, params=query_params, json=body_params or None, headers=headers)
                elif method == "PATCH":
                    response = await client.patch(url, params=query_params, json=body_params or None, headers=headers)
                elif method == "DELETE":
                    response = await client.delete(url, params=query_params, headers=headers)
                else:
                    response = await client.request(method, url, params=query_params, json=body_params, headers=headers)
                
                # Process response
                result = {
                    "status_code": response.status_code,
                    "url": str(response.url),
                }
                
                try:
                    result["data"] = response.json()
                except json.JSONDecodeError:
                    result["data"] = response.text
                
                return result
                
            except httpx.HTTPError as e:
                return {
                    "error": str(e),
                    "url": url,
                }
    
    return handler


def main():
    """Starts MCP server."""
    parser = argparse.ArgumentParser(
        description="Stargate MCP Server Runner",
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
      "args": ["/path/to/run_mcp_server.py", "/path/to/server.json"],
      "env": {
        "PYTHONPATH": "/path/to/stargate/src"
      }
    }
  }
}
        """
    )
    parser.add_argument(
        "server_json",
        help="Server JSON file path",
    )
    parser.add_argument(
        "--auth-token",
        help="API authentication token (Bearer)",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="MCP transport type (default: stdio)",
    )
    
    args = parser.parse_args()
    
    # Load server config
    try:
        config = load_server_config(args.server_json)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except json.JSONDecodeError as e:
        print(f"Error: JSON parse error: {e}", file=sys.stderr)
        sys.exit(1)
    
    # Create FastMCP server
    server_name = config.get("name", "Stargate MCP Server")
    mcp = FastMCP(server_name)
    
    base_url = config.get("base_url", "")
    auth_token = args.auth_token
    
    # Register tools
    for tool_config in config.get("tools", []):
        tool_name = tool_config["name"]
        tool_desc = tool_config.get("description", "")
        
        # Create handler
        handler = create_tool_handler(tool_config, base_url, auth_token)
        
        # Register tool
        mcp.tool(name=tool_name, description=tool_desc)(handler)
    
    # Info message (to stderr, since stdout is used for MCP)
    print(f"Starting MCP Server: {server_name}", file=sys.stderr)
    print(f"  Base URL: {base_url}", file=sys.stderr)
    print(f"  Tools: {len(config.get('tools', []))}", file=sys.stderr)
    print(f"  Transport: {args.transport}", file=sys.stderr)
    if auth_token:
        print(f"  Auth: Bearer token configured", file=sys.stderr)
    print("", file=sys.stderr)
    
    # Run server
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
