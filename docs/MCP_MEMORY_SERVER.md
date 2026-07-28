# ArchitectOS as an MCP Memory Server

ArchitectOS can act as a Model Context Protocol (MCP) **server** that exposes its
durable memory engine to external MCP clients such as **Cursor**, **GitHub
Copilot (Agent mode)**, and **Claude Desktop**. Those tools can then *search* and
*read* your ArchitectOS memory and *store* new lessons/decisions back into it.

This is the mirror of the built-in MCP Hub: the Hub makes ArchitectOS a *client*
of other MCP servers; this feature makes ArchitectOS a *server* for other agents.

## How it works

- Entry point: `mcp_memory_server.py` (repo root).
- Transport: MCP **stdio** — newline-delimited JSON-RPC 2.0 on stdin/stdout.
- Backing store: the same `data/architectos.db` the desktop app uses, so memory is
  shared. The app does **not** need to be running — the MCP server opens the store
  directly on demand.
- Only stderr is used for logs, so the protocol stream on stdout stays clean.

### Exposed tools

| Tool | Purpose | Key arguments |
| --- | --- | --- |
| `memory_search` | Return scored memory hits for a query | `query` (required), `project_id?`, `scope?`, `limit?` |
| `memory_context` | Build a ready-to-inject briefing (memory + open tasks + providers) | `query` (required), `project_id?`, `scope?`, `limit?` |
| `memory_add` | Persist a new memory (secrets auto-redacted) | `label` (required), `text` (required), `type?`, `scope?`, `project_id?`, `confidence?` |
| `memory_list_projects` | List projects for scoping | – |

## Requirements

- Python 3 available on PATH (the same interpreter used to run ArchitectOS).
- The absolute path to this repository.

Optional environment variable:

- `ARCHITECTOS_ROOT` — override the root whose `data/architectos.db` is used.
  Defaults to the ArchitectOS repository root, so the MCP server and the desktop
  app share one memory store by default.

Quick local check:

```bash
npm run mcp   # or: python mcp_memory_server.py
```

Then paste a line like the following on stdin and press Enter:

```json
{"jsonrpc":"2.0","id":1,"method":"tools/list"}
```

## Register in Cursor

Add an entry to `~/.cursor/mcp.json` (global) or `.cursor/mcp.json` (per project).
Use absolute paths.

```json
{
  "mcpServers": {
    "architectos-memory": {
      "command": "python3",
      "args": ["/ABSOLUTE/PATH/TO/ArchitectOS/mcp_memory_server.py"]
    }
  }
}
```

Reload Cursor. In Settings → MCP you should see `architectos-memory` with its four
tools. In chat the agent can now call e.g. `memory_search` before answering, or
`memory_add` to remember a decision.

## Register in GitHub Copilot (VS Code, Agent mode)

Create `.vscode/mcp.json` in your workspace:

```json
{
  "servers": {
    "architectos-memory": {
      "type": "stdio",
      "command": "python3",
      "args": ["/ABSOLUTE/PATH/TO/ArchitectOS/mcp_memory_server.py"]
    }
  }
}
```

Open the Copilot Chat **Agent** view, confirm the server is running, and the
`memory_*` tools become available to the model.

## Register in Claude Desktop

Edit the Claude Desktop config file:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "architectos-memory": {
      "command": "python3",
      "args": ["/ABSOLUTE/PATH/TO/ArchitectOS/mcp_memory_server.py"]
    }
  }
}
```

Restart Claude Desktop; the tools appear under the MCP tools menu.

## Notes and safety

- `memory_add` runs the same secret-redaction as the UI; credentials/tokens are
  stripped before anything is persisted.
- Memory text must contain at least 4 non-secret characters (empty/secret-only
  writes are rejected with a tool error rather than crashing the server).
- Concurrent access is fine for local single-user use (SQLite serializes writes);
  the desktop app and IDE agents can share the store simultaneously.
- Scope precedence still applies on search: narrower scopes (interface, project)
  outrank shared/global.
