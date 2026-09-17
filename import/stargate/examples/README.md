# Stargate Examples

This directory contains usage examples for Stargate.

## Quick Start

### 1. Install Dependencies

```bash
cd /path/to/stargate
pip install -e .
```

### 2. Run Examples (Generate JSON)

```bash
# Petstore API
python examples/petstore/convert_petstore.py

# GitHub API (manual definition)
python examples/github/convert_github.py

# E-Commerce API (custom definition)
python examples/custom/convert_custom.py

# Todo API (Postman Collection)
python examples/postman/convert_postman.py
```

Each example saves a `*_server.json` file to the `output/` folder.

### 3. Run MCP Server

```bash
# Petstore API
python examples/run_mcp_server.py examples/petstore/output/petstore_server.json

# GitHub API (with token)
python examples/run_mcp_server.py examples/github/output/github_server.json --auth-token ghp_xxx

# Equivalent, using the CLI
stargate serve -i examples/petstore/output/petstore_server.json
```

## Claude Desktop Integration

### Step 1: Generate JSON files

```bash
python examples/petstore/convert_petstore.py
python examples/github/convert_github.py
```

### Step 2: Edit Claude Desktop config file

**macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`

**Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

Example config (`claude_desktop_config.example.json`):

```json
{
  "mcpServers": {
    "petstore-api": {
      "command": "python",
      "args": [
        "/path/to/stargate/examples/run_mcp_server.py",
        "/path/to/stargate/examples/petstore/output/petstore_server.json"
      ],
      "env": {
        "PYTHONPATH": "/path/to/stargate/src"
      }
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
Converts Swagger Petstore API from OpenAPI.

```bash
python examples/petstore/convert_petstore.py
# Output: examples/petstore/output/petstore_server.json
```

### github/
Creates a portion of GitHub API from manual definition.

```bash
python examples/github/convert_github.py
# Output: examples/github/output/github_server.json
```

**Note:** GitHub API requires a Personal Access Token.

### custom/
Manually defines a sample e-commerce API.

```bash
python examples/custom/convert_custom.py
# Output: examples/custom/output/ecommerce_mcp_server.json
```

### postman/
Example conversion from Postman Collection format.

```bash
python examples/postman/convert_postman.py
# Output: examples/postman/output/todo_mcp_server.json
```

## run_mcp_server.py

This script runs an MCP server from a saved server.json file. It is a thin wrapper around `stargate.runtime.MCPServerRuntime`, so it behaves exactly like `stargate serve` (real tool input schemas, resources, SSRF checks); it exists so Claude Desktop can point at a plain Python file.

### Usage

```bash
python run_mcp_server.py <server_json_path> [OPTIONS]

Options:
  --auth-token TOKEN   API authentication token (Bearer)
  --transport TYPE     MCP transport type (stdio, sse, streamable-http) [default: stdio]
```

### Examples

```bash
# Basic usage
python run_mcp_server.py ./petstore/output/petstore_server.json

# With token
python run_mcp_server.py ./github/output/github_server.json --auth-token ghp_xxx

# With SSE transport
python run_mcp_server.py ./petstore/output/petstore_server.json --transport sse
```

## Directory Structure

```
examples/
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
