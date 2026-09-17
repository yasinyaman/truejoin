# Stargate

A Python tool that converts REST APIs into MCP (Model Context Protocol) servers.

## Features

- **Multiple Format Support**: OpenAPI/Swagger 3.x, Postman Collection v2.1, Manual definition
- **Automatic Format Detection**: Automatically detects input format
- **MCP Compatible Output**: MCP servers that AI agents (Claude, GPT-4, etc.) can use
- **CLI and Python API**: Can be used from command line or Python
- **Security**: SSRF protection, domain whitelist support

## Installation

```bash
pip install stargate
```

Or for development:

```bash
git clone https://github.com/stargate/stargate.git
cd stargate
pip install -e ".[dev]"
```

## Quick Start

### CLI Usage

```bash
# Create MCP server from OpenAPI
stargate convert -i openapi.json -o ./my-mcp-server

# Convert from URL
stargate convert -u https://fakestoreapi.com/docs-data

# From Postman Collection
stargate convert -i collection.json -f postman -o ./mcp-server

# Show API info
stargate info -i openapi.json
```

### Python API Usage

```python
from stargate import convert_to_mcp

# Convert from URL
result = convert_to_mcp(
    source="https://petstore3.swagger.io/api/v3/openapi.json",
    base_url="https://petstore3.swagger.io/api/v3"
)

print(f"Tools: {len(result.server.tools)}")
print(f"Resources: {len(result.server.resources)}")

# List tools
for tool in result.server.tools:
    print(f"  - {tool.name}: {tool.http_method} {tool.http_path}")
```

### Manual API Definition

```python
from stargate.adapters.parsers.manual import create_manual_api
from stargate.application.generators.server import generate_server

# Manual API definition
schema = create_manual_api(
    title="My API",
    base_url="https://api.example.com",
    endpoints=[
        {"path": "/users", "method": "GET", "name": "listUsers"},
        {"path": "/users/{id}", "method": "GET", "name": "getUser"},
        {"path": "/users", "method": "POST", "name": "createUser", "auth": True},
    ],
    auth={"type": "bearer"},
)

# Create MCP Server
result = generate_server(schema)
```

## Supported Formats

### OpenAPI/Swagger

- OpenAPI 3.0.x
- OpenAPI 3.1.x
- Swagger 2.0 (limited support)
- JSON and YAML formats

### Postman Collection

- Collection v2.1
- Folder structure support
- Environment variables
- Request/Response examples

### Manual Definition

- Simple JSON format
- Programmatic API creation
- Quick prototyping

## CLI Commands

### `stargate convert`

Converts API specification to MCP server.

```bash
stargate convert [OPTIONS]

Options:
  -i, --input PATH          Input file path
  -u, --url URL             API specification URL
  -f, --format FORMAT       Input format (openapi, postman, manual)
  -o, --output PATH         Output file or directory
  -b, --base-url URL        API base URL
  --include-deprecated      Include deprecated endpoints
  -t, --filter-tags TAG     Tag filter (can be used multiple times)
  --json-output             Output in JSON format
```

`--json-output` prints the MCP wire format (tools without their HTTP details) and cannot be fed back to `stargate serve`. Use `-o` to write a `server.json` that `serve` accepts.

### `stargate info`

Shows information about API specification.

```bash
stargate info [OPTIONS]

Options:
  -i, --input PATH    Input file path
  -u, --url URL       API specification URL
  -f, --format FORMAT Input format
```

### `stargate serve`

Runs the MCP server.

```bash
stargate serve [OPTIONS]

Options:
  -i, --input PATH        MCP server definition (JSON)
  -t, --transport TYPE    Transport type (stdio, sse, streamable-http)
  --auth-token TOKEN      API authentication token
```

### `stargate formats`

Lists supported formats.

```bash
stargate formats
```

## Python API

### `convert_to_mcp`

Main conversion function.

```python
from stargate import convert_to_mcp

result = convert_to_mcp(
    source="path/to/spec.json",  # URL, file path or dict
    base_url="https://api.example.com",  # Optional
    format="openapi",  # Optional (auto-detected)
    include_deprecated=False,
    filter_tags=["users", "products"],
)

# Result
result.success  # bool
result.server  # MCPServer
result.metadata  # dict
result.warnings  # list[str]
result.errors  # list[str]
```

### `parse_spec`

Parse only (without MCP conversion).

```python
from stargate import parse_spec

schema = parse_spec("path/to/spec.json")

# UnifiedAPISchema
schema.title
schema.version
schema.endpoints
schema.schemas
```

### Parsers

Parsers are pure: they take an already-loaded `dict`. Use `parse_spec` to read files and URLs (with an optional `format` to skip auto-detection).

```python
from stargate import parse_spec
from stargate.adapters.parsers import OpenAPIParser, PostmanParser, ManualParser

# From a file or URL, forcing the format
schema = parse_spec("openapi.json", format="openapi")
schema = parse_spec("collection.json", format="postman")

# From a dict you already have
schema = OpenAPIParser().parse(spec_dict)

# Manual
from stargate.adapters.parsers.manual import create_manual_api
schema = create_manual_api(title="My API", base_url="...", endpoints=[...])
```

### Generators

```python
from stargate.application.generators import MCPServerGenerator, ToolsGenerator, ResourcesGenerator

# Full server
generator = MCPServerGenerator(include_deprecated=True)
result = generator.generate(schema)

# Tools only
tools_gen = ToolsGenerator(filter_tags=["users"])
tools = tools_gen.generate(schema)

# Resources only
res_gen = ResourcesGenerator(include_schemas=True)
resources = res_gen.generate(schema)
```

## MCP Output Format

### Tools

Each API endpoint becomes an MCP tool:

```json
{
  "name": "listUsers",
  "description": "List all users | [GET /users] | Tags: users",
  "inputSchema": {
    "type": "object",
    "properties": {
      "page": {"type": "integer", "description": "Page number"},
      "limit": {"type": "integer", "description": "Items per page"}
    }
  }
}
```

### Resources

GET endpoints and schemas are served as MCP resources:

```json
{
  "name": "listUsers",
  "description": "List all users",
  "uri": "api://users",
  "mimeType": "application/json"
}
```

## Security

### SSRF Protection

Stargate provides protection against SSRF (Server-Side Request Forgery) attacks:

- Private IP ranges are blocked (10.x.x.x, 192.168.x.x, etc.)
- Localhost access is blocked
- Domain whitelist support

```python
result = convert_to_mcp(
    source="https://api.example.com/openapi.json",
    allowed_domains={"api.example.com", "docs.example.com"},
)
```

### Authentication

Supported authentication types:

- Bearer Token
- API Key (header or query)
- Basic Auth
- OAuth2

## Examples

Various usage examples are available in the `examples/` directory:

- `petstore/` - Petstore API conversion
- `github/` - GitHub API (manual definition)
- `postman/` - Postman Collection conversion
- `custom/` - Custom API definition

```bash
# Run examples
python examples/petstore/convert_petstore.py
python examples/github/convert_github.py
python examples/custom/convert_custom.py
python examples/postman/convert_postman.py
```

## Architecture

Stargate follows a hexagonal (ports & adapters) layout; dependencies point inwards.

```
src/stargate/
  domain/        pure models and rules: API/MCP models, JSON Schema helpers, URL policy
  ports/         Protocols the core needs: SpecParser, SpecLoader, ApiGateway,
                 ServerDefinitionRepository, MCPHost
  application/   use cases: ConversionService (+ generators), ToolInvoker, ResourceReader
  adapters/      implementations: parsers, spec loaders, httpx gateway, server.json
                 repository, MCP SDK host, click CLI, MCP wire serializer
  bootstrap.py   composition root for conversion (convert_to_mcp, parse_spec)
  runtime.py     composition root for serving (MCPServerRuntime)
```

`tests/test_architecture.py` enforces the layering from import statements (no extra tooling needed).

### Deprecated import paths

| Old | New |
|-----|-----|
| `stargate.converter` | `stargate` (or `stargate.bootstrap`) |
| `stargate.models.*` | `stargate.domain.api`, `stargate.domain.mcp` |
| `stargate.parsers.*` | `stargate.adapters.parsers.*` |
| `stargate.generators.*` | `stargate.application.generators.*` |
| `stargate.utils.*` | `stargate.domain.schema`, `stargate.domain.url_policy`, `stargate.adapters.httpx_gateway` |
| `stargate.server.runtime` | `stargate.runtime` |
| `stargate.cli` | `stargate.adapters.cli` |

The old paths still work but emit a `DeprecationWarning`.

## Development

### Running Tests

```bash
# All tests
pytest

# With coverage
pytest --cov=stargate

# Specific test file
pytest tests/test_parsers/test_openapi.py
```

### Code Format

```bash
# Lint with Ruff
ruff check src/

# Format with Ruff
ruff format src/
```

## License

Apache License 2.0

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add amazing feature'`)
4. Push the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request
