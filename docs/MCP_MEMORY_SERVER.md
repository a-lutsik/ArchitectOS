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
| `memory_list_projects` | List projects for scoping (`id`, `name`, `root_path`, `bound`) plus `create_new: true` | – |
| `memory_use_project` | Bind this folder to a project (writes `.architectos.json`) | `project_id` **or** unique `name`, `cwd?` |
| `project_create` | Create a project; from an unbound folder also binds it | `name?`, `root_path?`, `description?` (name or root_path required) |
| `memory_link` | Create a typed edge between two memory nodes (`SUPERSEDES` is bi-temporal) | `source`, `target`, `type` (required), `scope?`, `confidence?` |
| `source_create` | Register an external data source for a project (idempotent by name) | `name` (required), `project_id?` (required when the server is not bound), `kind?`, `config?` |
| `source_list` | List registered data sources | `project_id?` |
| `memory_history` | Event history of a node (created/updated/superseded/accessed) | `id` (required) |
| `memory_add_bulk` | Store a batch of nodes in one call; per-item errors don't abort the batch | `items` (required), `project_id?`, `scope?`, `source_id?`, `type?` |

One MCP server can serve every git folder. Bind order for a process:

1. `--project-id` / `ARCHITECTOS_PROJECT_ID` (explicit pin)
2. `.architectos.json` walked up from cwd (like `.git`)
3. `project_id_for_path(cwd)` (longest ArchitectOS `root_path` that contains cwd)
4. unbound

Unbound memory tools that need a project do **not** write to project `architectos`.
They return a tool error listing projects (`id` + `name`) and tell the agent to call
`memory_list_projects`, then `memory_use_project` (by name or id) or `project_create`
(create a new project, which auto-binds the folder).

```mermaid
---
config:
 layout: elk
 flowchart:
  curve: linear
  nodeSpacing: 40
  rankSpacing: 50
---
flowchart TB
  start["MCP tool needs a project"] --> pin{"`--project-id` / `ARCHITECTOS_PROJECT_ID`?"}
  pin -->|yes| bound["Use that project"]
  pin -->|no| file{"`.architectos.json` walked up from cwd?"}
  file -->|yes| bound
  file -->|no| path{"`project_id_for_path(cwd)`?"}
  path -->|yes| bound
  path -->|no| unbound["Tool error: list projects, then `memory_use_project` or `project_create`"]
```

The first session in a new folder should list projects, ask the user **by name**
(always offer to create a new project), then bind. After that the git/workspace
folder keeps `.architectos.json`:

```json
{"project_id":"project_…","name":"Shop"}
```

That file lives in the workspace folder (the process cwd, or the `cwd` argument), not
inside the ArchitectOS data root unless that folder *is* the workspace. A new
Cursor / Codex / Claude folder without that file is unbound again and asks. An
explicit `project_id` argument on a tool still overrides the bind. Env /
`--project-id` still beats the file.

Typical agent loop: if initialize `instructions` name a bound project id, call
`memory_turn` at the start of every user message (pack + fact capture). If the folder
is unbound, pick a project first — do not call `memory_turn` / `memory_search` /
`memory_add` until then. Still use `memory_search` / `memory_get` to narrow;
`memory_add` for an explicit lesson; `memory_feedback` to rate hits. Low-risk Lessons
from `memory_turn` auto-write to short-term memory; Decisions/Constraints stay in the
review queue (7-day TTL) with source **MCP** (not Ask). The same durable fact from an
Ask dialog is not queued twice.

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

Optional environment variables:

- `ARCHITECTOS_ROOT` — override the root whose `data/architectos.db` is used.
  Defaults to the ArchitectOS repository root (or the current working directory
  when running from an installed package), so the MCP server and the desktop app
  share one memory store by default.
- `ARCHITECTOS_PROJECT_ID` — optional explicit pin for this MCP process. It beats
  `.architectos.json` and path matching. Tools that omit `project_id` then read
  and write that project. The same pin is `--project-id` on the command line, and
  an explicit `project_id` argument still overrides it. You can share one MCP
  command across folders and let `.architectos.json` (or a first-session picker)
  choose the project; use the env pin only when you want the config itself to
  force an id. The id comes from `memory_list_projects` (field `id`, and
  `bound: true` on the bound row).

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
      "args": ["/ABSOLUTE/PATH/TO/ArchitectOS/mcp_memory_server.py"],
      "env": {
        "ARCHITECTOS_ROOT": "/ABSOLUTE/PATH/TO/ArchitectOS"
      }
    }
  }
}
```

Reuse one MCP command for every folder: do **not** set `ARCHITECTOS_PROJECT_ID`
per workspace as the only bind. The first session in a new folder lists projects,
the user picks a name (or creates one), and `.architectos.json` remembers the id.
Set `ARCHITECTOS_PROJECT_ID` (or pass `--project-id` in `args`) only when this
connection must stay pinned regardless of that file. Claude Code (`.mcp.json`)
and Codex (`[mcp_servers.architectos-memory.env]`) use the same optional pin.

Reload Cursor. In Settings → MCP you should see `architectos-memory` with its
tools. In chat, if the folder is bound, the agent should call `memory_turn` at
the start of each user message; if it is unbound, list/bind a project first.
`memory_add` still persists an explicit lesson or decision.

## Register in Claude Code

Add to `.mcp.json` in the project (or `~/.claude.json` for user scope):

```json
{
  "mcpServers": {
    "architectos-memory": {
      "command": "python3",
      "args": ["/ABSOLUTE/PATH/TO/ArchitectOS/mcp_memory_server.py"],
      "env": { "ARCHITECTOS_ROOT": "/ABSOLUTE/PATH/TO/ArchitectOS" }
    }
  }
}
```

Or with the installed package: `"command": "architectos-mcp"`, plus
`"env": {"ARCHITECTOS_ROOT": "/ABSOLUTE/PATH/TO/ArchitectOS"}`.
`ARCHITECTOS_PROJECT_ID` is optional; omit it unless you want this connection pinned.

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
      "args": ["/ABSOLUTE/PATH/TO/ArchitectOS/mcp_memory_server.py"],
      "env": { "ARCHITECTOS_ROOT": "/ABSOLUTE/PATH/TO/ArchitectOS" }
    }
  }
}
```

Open the Copilot Chat **Agent** view, confirm the server is running, and the
`memory_*` tools become available to the model. `ARCHITECTOS_PROJECT_ID` is
optional; a new folder without `.architectos.json` asks which project to bind.

## Register in Claude Desktop

Edit the Claude Desktop config file:

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

```json
{
  "mcpServers": {
    "architectos-memory": {
      "command": "python3",
      "args": ["/ABSOLUTE/PATH/TO/ArchitectOS/mcp_memory_server.py"],
      "env": { "ARCHITECTOS_ROOT": "/ABSOLUTE/PATH/TO/ArchitectOS" }
    }
  }
}
```

Restart Claude Desktop; the tools appear under the MCP tools menu.
`ARCHITECTOS_PROJECT_ID` is optional; omit it unless you want this connection pinned.

## Notes and safety

- `memory_add` runs the same secret-redaction as the UI; credentials/tokens are
  stripped before anything is persisted.
- Memory text must contain at least 4 non-secret characters (empty/secret-only
  writes are rejected with a tool error rather than crashing the server).
- Concurrent access is fine for local single-user use (SQLite serializes writes);
  the desktop app and IDE agents can share the store simultaneously.
- Scope precedence still applies on search: narrower scopes (interface, project)
  outrank shared/global.
