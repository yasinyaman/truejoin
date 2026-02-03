#!/usr/bin/env python3
"""Convert Petstore API to MCP server and save to JSON."""

import json
from pathlib import Path

from stargate import convert_to_mcp


def main():
    """Converts Petstore API and saves as JSON."""
    print("Converting Petstore API...")
    print("-" * 50)

    # Convert from URL
    result = convert_to_mcp(
        source="https://petstore3.swagger.io/api/v3/openapi.json",
        base_url="https://petstore3.swagger.io/api/v3",
    )

    if result.success:
        print(f"✓ Conversion successful!")
        print(f"  - API: {result.server.name} v{result.server.version}")
        print(f"  - Tools: {len(result.server.tools)}")
        print(f"  - Resources: {len(result.server.resources)}")
        print()

        # List tools
        print("MCP Tools:")
        for tool in result.server.tools[:10]:
            print(f"  • {tool.name}: {tool.http_method} {tool.http_path}")

        if len(result.server.tools) > 10:
            print(f"  ... and {len(result.server.tools) - 10} more tools")

        # Save JSON output
        output_dir = Path(__file__).parent / "output"
        output_dir.mkdir(exist_ok=True)

        # Server JSON
        server_file = output_dir / "petstore_server.json"
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

    # Warnings
    if result.warnings:
        print()
        print(f"Warnings ({len(result.warnings)}):")
        for warning in result.warnings[:5]:
            print(f"  ⚠ {warning}")


if __name__ == "__main__":
    main()
