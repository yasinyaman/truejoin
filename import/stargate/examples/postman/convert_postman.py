#!/usr/bin/env python3
"""Postman Collection'ı MCP sunucusuna dönüştürme örneği."""

import json
from pathlib import Path

from stargate import convert_to_mcp
from stargate.bootstrap import default_repository


def main():
    """Örnek Postman Collection'ı dönüştürür."""
    print("Postman Collection dönüştürülüyor...")
    print("-" * 50)

    # Örnek Postman Collection
    postman_collection = {
        "info": {
            "_postman_id": "example-collection-001",
            "name": "Todo API Collection",
            "description": "Basit bir Todo API'si için Postman Collection",
            "schema": "https://schema.getpostman.com/json/collection/v2.1.0/collection.json",
            "version": "1.0.0",
        },
        "variable": [
            {"key": "baseUrl", "value": "https://api.todos.example.com"},
            {"key": "authToken", "value": "your-token-here"},
        ],
        "auth": {
            "type": "bearer",
            "bearer": [{"key": "token", "value": "{{authToken}}"}],
        },
        "item": [
            {
                "name": "Todos",
                "description": "Todo işlemleri",
                "item": [
                    {
                        "name": "List Todos",
                        "request": {
                            "method": "GET",
                            "url": {
                                "raw": "{{baseUrl}}/todos",
                                "host": ["{{baseUrl}}"],
                                "path": ["todos"],
                                "query": [
                                    {
                                        "key": "completed",
                                        "value": "false",
                                        "description": "Filter by completion status",
                                    },
                                    {
                                        "key": "limit",
                                        "value": "10",
                                        "description": "Number of items to return",
                                    },
                                ],
                            },
                            "description": "Get all todos with optional filtering",
                        },
                        "response": [
                            {
                                "name": "Success",
                                "code": 200,
                                "body": json.dumps(
                                    [
                                        {
                                            "id": 1,
                                            "title": "Buy groceries",
                                            "completed": False,
                                        },
                                        {
                                            "id": 2,
                                            "title": "Walk the dog",
                                            "completed": True,
                                        },
                                    ]
                                ),
                            }
                        ],
                    },
                    {
                        "name": "Get Todo",
                        "request": {
                            "method": "GET",
                            "url": {
                                "raw": "{{baseUrl}}/todos/:todoId",
                                "host": ["{{baseUrl}}"],
                                "path": ["todos", ":todoId"],
                                "variable": [
                                    {
                                        "key": "todoId",
                                        "value": "1",
                                        "description": "Todo ID",
                                    }
                                ],
                            },
                        },
                    },
                    {
                        "name": "Create Todo",
                        "request": {
                            "method": "POST",
                            "url": {
                                "raw": "{{baseUrl}}/todos",
                                "host": ["{{baseUrl}}"],
                                "path": ["todos"],
                            },
                            "header": [
                                {"key": "Content-Type", "value": "application/json"}
                            ],
                            "body": {
                                "mode": "raw",
                                "raw": json.dumps(
                                    {
                                        "title": "New todo item",
                                        "description": "Description of the todo",
                                        "completed": False,
                                    }
                                ),
                                "options": {"raw": {"language": "json"}},
                            },
                        },
                    },
                    {
                        "name": "Update Todo",
                        "request": {
                            "method": "PUT",
                            "url": {
                                "raw": "{{baseUrl}}/todos/:todoId",
                                "host": ["{{baseUrl}}"],
                                "path": ["todos", ":todoId"],
                                "variable": [{"key": "todoId", "value": "1"}],
                            },
                            "body": {
                                "mode": "raw",
                                "raw": json.dumps({"completed": True}),
                                "options": {"raw": {"language": "json"}},
                            },
                        },
                    },
                    {
                        "name": "Delete Todo",
                        "request": {
                            "method": "DELETE",
                            "url": {
                                "raw": "{{baseUrl}}/todos/:todoId",
                                "host": ["{{baseUrl}}"],
                                "path": ["todos", ":todoId"],
                                "variable": [{"key": "todoId", "value": "1"}],
                            },
                        },
                    },
                ],
            },
            {
                "name": "Users",
                "description": "Kullanıcı işlemleri",
                "item": [
                    {
                        "name": "Get Current User",
                        "request": {
                            "method": "GET",
                            "url": {
                                "raw": "{{baseUrl}}/users/me",
                                "host": ["{{baseUrl}}"],
                                "path": ["users", "me"],
                            },
                        },
                    },
                    {
                        "name": "Update Profile",
                        "request": {
                            "method": "PATCH",
                            "url": {
                                "raw": "{{baseUrl}}/users/me",
                                "host": ["{{baseUrl}}"],
                                "path": ["users", "me"],
                            },
                            "body": {
                                "mode": "raw",
                                "raw": json.dumps({"name": "New Name"}),
                                "options": {"raw": {"language": "json"}},
                            },
                        },
                    },
                ],
            },
        ],
    }

    # Dönüştür
    result = convert_to_mcp(postman_collection, format="postman")

    if result.success:
        print(f"✓ Dönüşüm başarılı!")
        print(f"  - Collection: {result.server.name}")
        print(f"  - Tools: {len(result.server.tools)}")
        print(f"  - Resources: {len(result.server.resources)}")
        print()

        # Folder'lara göre grupla
        print("MCP Tools (folder'lara göre):")
        current_folder = None
        for tool in result.server.tools:
            # Tag'den folder adını çıkar
            folder = None
            if "Todos" in str(tool.description):
                folder = "Todos"
            elif "Users" in str(tool.description):
                folder = "Users"

            if folder != current_folder:
                current_folder = folder
                if folder:
                    print(f"\n  [{folder}]")

            print(f"    • {tool.name}: {tool.http_method} {tool.http_path}")

        print()
        print("MCP Resources:")
        for resource in result.server.resources:
            print(f"  • {resource.name}: {resource.uri}")

        # JSON çıktı kaydet
        output_path = Path(__file__).parent / "output"
        output_path.mkdir(exist_ok=True)

        server_json = output_path / "todo_mcp_server.json"
        default_repository().save(result.server, server_json)
        print(f"\n✓ Server JSON kaydedildi: {server_json}")

    else:
        print(f"✗ Dönüşüm başarısız!")
        for error in result.errors:
            print(f"  - {error}")


if __name__ == "__main__":
    main()
