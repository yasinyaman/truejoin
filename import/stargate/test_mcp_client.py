#!/usr/bin/env python3
"""MCP sunucusunu test etmek için basit istemci."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, "src")

from stargate.adapters.json_server_repository import JsonServerRepository
from stargate.runtime import MCPServerRuntime


async def test_server():
    """MCP sunucusunu test et."""
    
    # server.json yükle
    server = JsonServerRepository().load(Path("server.json"))
    tools, resources = server.tools, server.resources

    runtime = MCPServerRuntime(server)
    
    print("=" * 60)
    print(f"MCP Server: {server.name} v{server.version}")
    print(f"Base URL: {server.base_url}")
    print(f"Tools: {len(tools)}")
    print(f"Resources: {len(resources)}")
    print("=" * 60)
    
    # Tool listesi
    print("\n📦 Mevcut Tool'lar:")
    for tool in tools:
        print(f"  - {tool.name}: {tool.http_method} {tool.http_path}")
    
    # Resource listesi
    print("\n📄 Mevcut Resource'lar:")
    for resource in resources:
        print(f"  - {resource.name}: {resource.uri}")
    
    print("\n" + "=" * 60)
    print("🧪 Test İstekleri:")
    print("=" * 60)
    
    async with runtime:
        # Test 1: Tüm ürünleri getir
        print("\n1️⃣  getAllProducts çağrılıyor...")
        try:
            result = await runtime.call_tool("getAllProducts", {})
            print(f"   Status: {result.get('status_code')}")
            data = result.get("data", [])
            if isinstance(data, list):
                print(f"   Sonuç: {len(data)} ürün bulundu")
                if data:
                    print(f"   İlk ürün: {data[0].get('title', 'N/A')}")
            else:
                print(f"   Sonuç: {data}")
        except Exception as e:
            print(f"   Hata: {e}")
        
        # Test 2: Tek ürün getir
        print("\n2️⃣  getProductById(id=1) çağrılıyor...")
        try:
            result = await runtime.call_tool("getProductById", {"id": 1})
            print(f"   Status: {result.get('status_code')}")
            data = result.get("data", {})
            if isinstance(data, dict):
                print(f"   Ürün: {data.get('title', 'N/A')}")
                print(f"   Fiyat: ${data.get('price', 'N/A')}")
                print(f"   Kategori: {data.get('category', 'N/A')}")
        except Exception as e:
            print(f"   Hata: {e}")
        
        # Test 3: Tüm kullanıcıları getir
        print("\n3️⃣  getAllUsers çağrılıyor...")
        try:
            result = await runtime.call_tool("getAllUsers", {})
            print(f"   Status: {result.get('status_code')}")
            data = result.get("data", [])
            if isinstance(data, list):
                print(f"   Sonuç: {len(data)} kullanıcı bulundu")
        except Exception as e:
            print(f"   Hata: {e}")
        
        # Test 4: Sepetleri getir
        print("\n4️⃣  getAllCarts çağrılıyor...")
        try:
            result = await runtime.call_tool("getAllCarts", {})
            print(f"   Status: {result.get('status_code')}")
            data = result.get("data", [])
            if isinstance(data, list):
                print(f"   Sonuç: {len(data)} sepet bulundu")
        except Exception as e:
            print(f"   Hata: {e}")
        
        # Test 5: Yeni ürün ekle (POST)
        print("\n5️⃣  addProduct çağrılıyor (POST)...")
        try:
            new_product = {
                "title": "Test Ürünü",
                "price": 99.99,
                "description": "Bu bir test ürünüdür",
                "category": "electronics",
                "image": "https://example.com/image.jpg"
            }
            result = await runtime.call_tool("addProduct", new_product)
            print(f"   Status: {result.get('status_code')}")
            print(f"   Sonuç: {result.get('data')}")
        except Exception as e:
            print(f"   Hata: {e}")
    
    print("\n" + "=" * 60)
    print("✅ Test tamamlandı!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(test_server())
