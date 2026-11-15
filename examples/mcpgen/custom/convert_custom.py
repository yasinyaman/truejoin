#!/usr/bin/env python3
"""Özel API tanımı ile MCP sunucusu oluşturma örneği."""

from pathlib import Path

from truejoin.mcpgen import convert_to_mcp
from truejoin.bootstrap import server_repository


def main():
    """Özel bir API tanımını MCP'ye dönüştürür."""
    print("Özel API dönüştürülüyor...")
    print("-" * 50)

    # Manuel API tanımı
    custom_api = {
        "format": "manual",
        "title": "E-Commerce API",
        "version": "2.0.0",
        "description": "Örnek e-ticaret API'si",
        "base_url": "https://api.ecommerce.example.com/v2",
        "auth": {
            "type": "bearer",
            "description": "JWT Bearer token",
        },
        "tags": ["products", "orders", "users"],
        "endpoints": [
            # Products
            {
                "path": "/products",
                "method": "GET",
                "name": "listProducts",
                "summary": "Ürünleri listele",
                "description": "Tüm ürünleri sayfalanmış olarak döndürür",
                "parameters": [
                    {
                        "name": "page",
                        "in": "query",
                        "type": "integer",
                        "description": "Sayfa numarası",
                    },
                    {
                        "name": "limit",
                        "in": "query",
                        "type": "integer",
                        "description": "Sayfa başına ürün sayısı",
                    },
                    {
                        "name": "category",
                        "in": "query",
                        "type": "string",
                        "description": "Kategori filtresi",
                    },
                    {
                        "name": "min_price",
                        "in": "query",
                        "type": "number",
                        "description": "Minimum fiyat",
                    },
                    {
                        "name": "max_price",
                        "in": "query",
                        "type": "number",
                        "description": "Maksimum fiyat",
                    },
                ],
                "response": {
                    "type": "object",
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "integer"},
                                    "name": {"type": "string"},
                                    "price": {"type": "number"},
                                    "category": {"type": "string"},
                                },
                            },
                        },
                        "total": {"type": "integer"},
                        "page": {"type": "integer"},
                    },
                },
                "tags": ["products"],
            },
            {
                "path": "/products/{id}",
                "method": "GET",
                "name": "getProduct",
                "summary": "Ürün detayı",
                "tags": ["products"],
            },
            {
                "path": "/products",
                "method": "POST",
                "name": "createProduct",
                "summary": "Yeni ürün oluştur",
                "body": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "price": {"type": "number"},
                            "description": {"type": "string"},
                            "category": {"type": "string"},
                            "stock": {"type": "integer"},
                        },
                        "required": ["name", "price"],
                    }
                },
                "auth": True,
                "tags": ["products"],
            },
            # Orders
            {
                "path": "/orders",
                "method": "GET",
                "name": "listOrders",
                "summary": "Siparişleri listele",
                "parameters": [
                    {"name": "status", "in": "query", "type": "string"},
                ],
                "auth": True,
                "tags": ["orders"],
            },
            {
                "path": "/orders/{id}",
                "method": "GET",
                "name": "getOrder",
                "summary": "Sipariş detayı",
                "auth": True,
                "tags": ["orders"],
            },
            {
                "path": "/orders",
                "method": "POST",
                "name": "createOrder",
                "summary": "Yeni sipariş oluştur",
                "body": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "items": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "product_id": {"type": "integer"},
                                        "quantity": {"type": "integer"},
                                    },
                                },
                            },
                            "shipping_address": {"type": "string"},
                        },
                        "required": ["items"],
                    }
                },
                "auth": True,
                "tags": ["orders"],
            },
            # Users
            {
                "path": "/users/me",
                "method": "GET",
                "name": "getCurrentUser",
                "summary": "Mevcut kullanıcı bilgisi",
                "auth": True,
                "tags": ["users"],
            },
            {
                "path": "/users/me",
                "method": "PATCH",
                "name": "updateCurrentUser",
                "summary": "Kullanıcı bilgilerini güncelle",
                "body": {
                    "schema": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "email": {"type": "string"},
                            "phone": {"type": "string"},
                        },
                    }
                },
                "auth": True,
                "tags": ["users"],
            },
        ],
    }

    # Dönüştür
    result = convert_to_mcp(custom_api)

    if result.success:
        print(f"✓ Dönüşüm başarılı!")
        print(f"  - API: {result.server.name} v{result.server.version}")
        print(f"  - Tools: {len(result.server.tools)}")
        print(f"  - Resources: {len(result.server.resources)}")
        print()

        # Tag'lere göre grupla
        tools_by_tag = {}
        for tool in result.server.tools:
            # Tag'i description'dan çıkar
            for tag in ["products", "orders", "users"]:
                if tag in tool.description.lower():
                    tools_by_tag.setdefault(tag, []).append(tool)
                    break

        print("MCP Tools (tag'lere göre):")
        for tag, tools in tools_by_tag.items():
            print(f"\n  [{tag.upper()}]")
            for tool in tools:
                auth_marker = "🔒" if tool.security else "🔓"
                print(f"    {auth_marker} {tool.name}: {tool.http_method} {tool.http_path}")

        # JSON çıktı kaydet
        output_path = Path(__file__).parent / "output"
        output_path.mkdir(exist_ok=True)

        server_json = output_path / "ecommerce_mcp_server.json"
        server_repository().save(result.server, server_json)
        print(f"\n✓ Server JSON kaydedildi: {server_json}")

    else:
        print(f"✗ Dönüşüm başarısız!")
        for error in result.errors:
            print(f"  - {error}")


if __name__ == "__main__":
    main()
