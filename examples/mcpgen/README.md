# MCP converter examples (`truejoin mcp`)

Examples of turning an API description (OpenAPI, Postman, or one written in Python)
into an MCP server definition, and serving it. The converter is the Stargate part of
truejoin: `truejoin mcp convert` on the command line, `truejoin.mcpgen` in Python.

## Quick Start

### 1. Install

```bash
cd /path/to/truejoin
uv sync
```

### 2. Run the examples (generate the JSON)

From the repository root:

```bash
# Petstore API (OpenAPI, fetched from petstore3.swagger.io)
uv run python examples/mcpgen/petstore/convert_petstore.py

# GitHub API (manual definition)
uv run python examples/mcpgen/github/convert_github.py

# E-Commerce API (custom definition)
uv run python examples/mcpgen/custom/convert_custom.py

# Todo API (Postman Collection)
uv run python examples/mcpgen/postman/convert_postman.py
```

Each example saves a `*_server.json` file to its own `output/` folder.

The command-line equivalent of a conversion is:

```bash
uv run truejoin mcp convert -i spec.json -o ./out           # or -u <url>, -f postman
uv run truejoin mcp info -i spec.json                       # what a spec holds, without converting
```

### 3. Run an MCP server

```bash
# Petstore API
uv run python examples/mcpgen/run_mcp_server.py examples/mcpgen/petstore/output/petstore_server.json

# GitHub API (with token)
uv run python examples/mcpgen/run_mcp_server.py examples/mcpgen/github/output/github_server.json --auth-token ghp_xxx
```

A server.json is served only through `run_mcp_server.py`: the `truejoin` CLI converts
and inspects, it does not serve these definitions.

## Claude Desktop Integration

### Step 1: Generate the JSON files

```bash
uv run python examples/mcpgen/petstore/convert_petstore.py
uv run python examples/mcpgen/github/convert_github.py
```

### Step 2: Edit the Claude Desktop config file

**macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`

**Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

Example config (`claude_desktop_config.example.json`):

```json
{
  "mcpServers": {
    "petstore-api": {
      "command": "uv",
      "args": [
        "run",
        "--directory",
        "/path/to/truejoin",
        "python",
        "examples/mcpgen/run_mcp_server.py",
        "examples/mcpgen/petstore/output/petstore_server.json"
      ]
    }
  }
}
```

### Step 3: Restart Claude Desktop

After saving the config, close and reopen Claude Desktop.

### Step 4: Use in Claude

Now you can ask Claude things like:

- "List all pets in the Petstore"
- "Get pet with ID 1"
- "Create a new pet: name Max, type dog"

## Examples

### petstore/
Converts the Swagger Petstore API from OpenAPI.

```bash
uv run python examples/mcpgen/petstore/convert_petstore.py
# Output: examples/mcpgen/petstore/output/petstore_server.json
```

### github/
Creates a portion of the GitHub API from a manual definition.

```bash
uv run python examples/mcpgen/github/convert_github.py
# Output: examples/mcpgen/github/output/github_server.json
```

**Note:** the GitHub API requires a Personal Access Token.

### custom/
Manually defines a sample e-commerce API.

```bash
uv run python examples/mcpgen/custom/convert_custom.py
# Output: examples/mcpgen/custom/output/ecommerce_mcp_server.json
```

### postman/
Converts a Postman Collection.

```bash
uv run python examples/mcpgen/postman/convert_postman.py
# Output: examples/mcpgen/postman/output/todo_mcp_server.json
```

## run_mcp_server.py

Runs an MCP server from a saved server.json file. It is a thin wrapper around
`truejoin.runtime_mcpgen.MCPServerRuntime` (real tool input schemas, resources, SSRF
checks); it exists so Claude Desktop can point at a plain Python file.

### Usage

```bash
uv run python examples/mcpgen/run_mcp_server.py <server_json_path> [OPTIONS]

Options:
  --auth-token TOKEN   API authentication token (Bearer)
  --transport TYPE     MCP transport type (stdio, sse, streamable-http) [default: stdio]
```

### Examples

```bash
# Basic usage
uv run python examples/mcpgen/run_mcp_server.py examples/mcpgen/petstore/output/petstore_server.json

# With token
uv run python examples/mcpgen/run_mcp_server.py examples/mcpgen/github/output/github_server.json --auth-token ghp_xxx

# With SSE transport
uv run python examples/mcpgen/run_mcp_server.py examples/mcpgen/petstore/output/petstore_server.json --transport sse
```

## Directory Structure

```
examples/mcpgen/
├── README.md                           # This file
├── run_mcp_server.py                   # MCP server runner
├── claude_desktop_config.example.json  # Example Claude Desktop config
├── petstore/
│   ├── convert_petstore.py
│   └── output/
│       └── petstore_server.json
├── github/
│   ├── convert_github.py
│   └── output/
│       └── github_server.json
├── custom/
│   ├── convert_custom.py
│   └── output/
│       └── ecommerce_mcp_server.json
└── postman/
    ├── convert_postman.py
    └── output/
        └── todo_mcp_server.json
```
