# ArchitectOS as an MCP Memory Server

ArchitectOS can act as a Model Context Protocol (MCP) **server** that exposes its
durable memory engine to external MCP clients such as **Cursor**, **GitHub
Copilot (Agent mode)**, and **Claude Desktop**. Those tools can then *search* and
*read* your ArchitectOS memory and *store* new lessons/decisions back into it.

This is the mirror of the built-in MCP Hub: the Hub makes ArchitectOS a *client*
of other MCP servers; this feature makes ArchitectOS a *server* for other agents.

> MCP only fires when the model decides to call a tool, so an agent that stays
> silent contributes nothing. For Cursor, Claude Code and Codex CLI you can also
> install lifecycle hooks that capture every turn regardless — see
> [AGENT_HOOKS.md](AGENT_HOOKS.md). Hooks and MCP share one store and one set of
> review rules, and are meant to run together.

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
| `memory_turn` | Per-turn pack + fact capture (call at the start of every user message) | `user_text` (required), `assistant_text?`, `project_id?`, `scope?`, `limit?` |
| `memory_add` | Persist a new memory; multi-fact dumps are split. Secrets auto-redacted | `label` (required), `text` (required), `type?`, `scope?`, `project_id?`, `confidence?` |
| `memory_get` | Fetch one memory node in full by id (untruncated text, metadata, evidence, neighbors) | `id` (required), `include_neighbors?` |
| `memory_feedback` | Rate retrieved hits so ranking improves over time | `rating` (1 or -1, required), `hit_ids?`, `query?`, `note?`, `project_id?` |
| `memory_list_projects` | List projects for scoping | – |
| `project_create` | Create a project; without `root_path` a knowledge-only project (no local folder) | `name?`, `root_path?`, `description?` (name or root_path required) |
| `memory_link` | Create a typed edge between two memory nodes (`SUPERSEDES` is bi-temporal) | `source`, `target`, `type` (required), `scope?`, `confidence?` |
| `source_create` | Register an external data source for a project (idempotent by name) | `project_id`, `name` (required), `kind?`, `config?` |
| `source_list` | List registered data sources | `project_id?` |
| `memory_history` | Event history of a node (created/updated/superseded/accessed) | `id` (required) |
| `memory_add_bulk` | Store a batch of nodes in one call; per-item errors don't abort the batch | `items` (required), `project_id?`, `scope?`, `source_id?`, `type?` |

Typical agent loop: initialize `instructions` already include a short stable-memory briefing.
At the start of every user message call `memory_turn` with that message (pack + fact
capture). Still use `memory_search` / `memory_get` to narrow; `memory_add` for an
explicit lesson; `memory_feedback` to rate hits. Low-risk Lessons from `memory_turn`
auto-write to short-term memory; Decisions/Constraints stay in the review queue (7-day TTL)
with source **MCP** (not Ask). The same durable fact from an Ask dialog is not queued twice.

### Exposed resources (management views)

| URI | Content |
| --- | --- |
| `memory://briefing` | Stable memory (pinned, constraints, long-term) plus startup instructions — inject at session start so the agent does not wait for `memory_search` |
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
tools. In chat the agent should call `memory_turn` at the start of each user
message; `memory_add` still persists an explicit lesson or decision.

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
