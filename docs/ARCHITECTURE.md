# ArchitectOS Architecture

ArchitectOS is a local-first AI operating system for software engineers. The center of gravity is persistent project memory, not a disposable chat transcript.

## Borrowed Ideas From MF0-1984

The app adopts useful local-first patterns from MF0-1984: single-page workspace, multi-provider catalog, structured workflows, memory graph, analytics, favorites, dialogs, and profile backup/restore bundles. ArchitectOS adapts those patterns to software engineering work: project scans, architecture memory, context packs, task board, provider routing, and release-ready local startup.

## Runtime

```text
Browser UI
  -> Python HTTP API
     -> ArchitectOSService
        -> SQLiteMemoryRepository
        -> HybridSearchStrategy
        -> ProviderRouter + RouterPolicy + ProviderAdapter implementations
        -> MultiAgentOrchestrator
        -> MCPManager + MCPClient (MCP stdio JSON-RPC)
        -> CodeIntelligenceManager + LSPClient (LSP stdio JSON-RPC)
        -> SecurityPolicy
     -> SQLite + Markdown evidence

External MCP client (Cursor / Copilot / Claude Desktop)
  -> mcp_memory_server.py (MCP stdio server)
     -> ArchitectOSService  (same data/architectos.db)
```

The MCP direction is bidirectional: ArchitectOS is an MCP *client* (connects out to
Filesystem/GitHub/Jira/... servers) and, via `architectos.mcp_server`, an MCP *server*
that exposes the memory engine so IDE agents can read and write ArchitectOS memory.

## Storage

`data/architectos.db` is the source of truth. Memory evidence is also written under `memory/evidence` as markdown. Release packages exclude `data/`, `memory/`, `dist/`, and cache files.

## Core Tables

- `projects`
- `memory_nodes`
- `memory_edges`
- `memory_candidates`
- `tasks`
- `chat_sessions`
- `providers`
- `provider_runs`
- `settings`

## Implemented

- Local-first Python HTTP app with static workspace UI.
- SQLite memory, markdown evidence, scoped search, context builder, graph expansion, and bundle import/export.
- MF0-style canvas graph with filters, focus, drag, actions, pinning, merge, edge creation, and path explanation.
- Provider routing for local memory, OpenAI, Anthropic, OpenRouter, Ollama, Codex CLI, Claude Code, and Gemini CLI.
- Smart AI Router: weighted cost/speed/quality/availability scoring, task-role classification, strategies, and a routing preview.
- Multi-Agent orchestration: role agents (code/architecture/docs/review) with an optional synthesis pass, reusing routing and audit.
- MCP Hub: real MCP stdio JSON-RPC client, server registry (Filesystem, GitHub, Azure DevOps, Jira, Slack, Confluence, Granola), test/list-tools/call with approval gating.
- MCP Memory Server: exposes the memory engine over MCP stdio so Cursor, Copilot, and Claude Desktop can search/read and add memory (`mcp_memory_server.py`; see `docs/MCP_MEMORY_SERVER.md`).
- Code Intelligence: real LSP stdio JSON-RPC client for Python, TypeScript, Go, Rust, and Java with document symbols and hover; no custom parsers.
- Streaming responses, CLI approval/cancel/workdir safety, provider audit trail, model discovery, and setup checks.
- Workflow engine, memory ingestion/promotion, project workspace file browsing, selected-file context, and git diff.
- Desktop-style launcher with browser open, port conflict handling, startup scripts, runtime state, and release package builder.
- Security policy for prompt/result/audit/persistence redaction.
- E2E, fake adapter, release package, and startup/configuration tests.

## Future Options

- Native Electron/Tauri shell or signed platform installers.
- React/TypeScript migration if the static SPA outgrows dependency-free maintenance.
- Specialized ingestion templates for ADRs, issue trackers, commits, and meeting notes.
- Team sync/cloud backup after local-first behavior is stable.


See `docs/PRODUCTION.md` for production operations, readiness, backups, and security headers.
