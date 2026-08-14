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
| `memory_search` | Return scored memory hits for a query | `query`, `project_id?`, `scope?`, `limit?`, `mode?` (`search`/`list`), `filters?` (exact, `!=X`, `a\|b`, lists) |
| `memory_context` | Build a ready-to-inject briefing (memory + open tasks + providers) | `query` (required), `project_id?`, `scope?`, `limit?` |
| `memory_add` | Persist a new memory (secrets auto-redacted) | `label` (required), `text` (required), `type?`, `scope?`, `project_id?`, `confidence?` |
| `memory_get` | Fetch one memory node in full by id (untruncated text, metadata, evidence, neighbors) | `id` (required), `include_neighbors?` |
| `memory_feedback` | Rate retrieved hits so ranking improves over time | `rating` (1 or -1, required), `hit_ids?`, `query?`, `note?`, `project_id?` |
| `memory_list_projects` | List projects for scoping | – |

Typical agent loop: `memory_search` → `memory_get` for the full record → answer →
`memory_feedback` on the hits that were (not) useful → `memory_add` for new lessons.

### Exposed resources (management views)

| URI | Content |
| --- | --- |
| `memory://projects` | Projects available for scoping (JSON) |
| `memory://review` | Memory candidates awaiting review (promote/reject) |
| `memory://nodes/{id}` | Resource template: full node + evidence + neighbors |

## Requirements

- Python 3 available on PATH (the same interpreter used to run ArchitectOS).
- The absolute path to this repository — **or** an installed package (see below).

Optional environment variable:

- `ARCHITECTOS_ROOT` — override the root whose `data/architectos.db` is used.
  Defaults to the ArchitectOS repository root (or the current working directory
  when running from an installed package), so the MCP server and the desktop app
  share one memory store by default.

### Install as a package (optional)

```bash
pip install -e .
architectos-mcp   # console script; uses $ARCHITECTOS_ROOT or the current directory
```

This lets MCP clients launch `architectos-mcp` without absolute script paths.

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

Reload Cursor. In Settings → MCP you should see `architectos-memory` with its
tools. In chat the agent can now call e.g. `memory_search` before answering, or
`memory_add` to remember a decision.

## Register in Claude Code

Add to `.mcp.json` in the project (or `~/.claude.json` for user scope):

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

Or with the installed package: `"command": "architectos-mcp"`, plus
`"env": {"ARCHITECTOS_ROOT": "/ABSOLUTE/PATH/TO/ArchitectOS"}`.

## Register in Junie / JetBrains AI Assistant (IntelliJ)

Settings → Tools → AI Assistant → Model Context Protocol (MCP) → Add, or edit the
MCP config XML with the same stdio command/args as above. Once connected, Junie
can search and write team memory directly from IntelliJ.

## Team setup

- **Shared memory**: point every team member's MCP client at a checkout whose
  `data/architectos.db` is synced/shared (or run one server per machine against a
  shared store location via `ARCHITECTOS_ROOT`).
- **Per-developer memory**: each developer uses their own checkout; merge happens
  through the desktop app's review queue.
- SQLite serializes writes, so the desktop app and several IDE agents can share
  the store concurrently for local use.

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
