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
        -> CouncilOrchestrator
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
- Agent tools: OpenAI/Azure providers receive the tool catalog as native function schemas (`ProviderRequest.tools`) and their calls are replayed through the same `tool_calls` loop other providers reach via the text protocol. Azure DevOps ops go through `call_ado_tool`, which targets the current `@azure-devops/mcp` action-based tools (`wit_work_item`, `wit_query`, `repo_repository`, `repo_pull_request`, `wiki`) and retries the pre-consolidation names on older servers; empty payloads and `isError` results surface as tool failures instead of silent nulls.
- Azure Repos in chat: `repo_search_commits`, `repo_pull_requests_for_commit`, `repo_get_pull_request`, `repo_list_pull_request_comments`, `repo_read_file_at`, and `repo_list_repositories` let the agent trace a commit hash to its pull request, changed files, linked work items, and the file content at that revision, then diff it against the working copy via `fs_read`. They ship with the Azure DevOps MCP server and are read-only.
- MCP Memory Server: exposes the memory engine over MCP stdio so Cursor, Copilot, and Claude Desktop can search/read and add memory (`mcp_memory_server.py`; see `docs/MCP_MEMORY_SERVER.md`).
- Code Intelligence: real LSP stdio JSON-RPC client for Python, TypeScript, Go, Rust, and Java with document symbols and hover; no custom parsers.
- Persistent code graph: `scan_project` runs `CodeGraphIngestor` (`code_graph.py`) to extract `Symbol` nodes (functions/methods/classes with signature + docstring) and code edges — `DEFINES`/`CONTAINS`/`IMPORTS` (EXTRACTED) and best-effort `CALLS` (INFERRED) — into the same `memory_nodes`/`memory_edges` graph, giving communities/PageRank/retrieval a real code substrate. Offline (Python `ast` + regex for JS/TS), idempotent, and pruned on re-scan. Agents query it via the `code_neighbors` and `code_impact` MCP tools; symbol embeddings are optional (`embed_symbols` policy). See `docs/CODE_GRAPH.md`.
- Streaming responses, CLI approval/cancel/workdir safety, provider audit trail, model discovery, and setup checks.
- Session-based chat memory: messages stay in `chat_sessions` while active; on manual End session or idle timeout (`chat_session_idle_minutes`) ArchitectOS summarizes durable facts into one `chat_session_summary` memory candidate (`POST /api/chats/{id}/finalize`).
- Workflow engine, memory ingestion/promotion, project workspace file browsing, selected-file context, and git diff.
- Desktop-style launcher with browser open, port conflict handling, startup scripts, runtime state, and release package builder.
- Security policy for prompt/result/audit/persistence redaction.
- LLM-assisted memory linking (`suggest_memory_links`, `POST /api/graph/suggest-links`): picks the least-linked active content nodes, finds unlinked pairs by token similarity (`min_similarity`, default 0.22), asks the configured LLM for a JSON verdict (`related`, `edge_type`, `reason`, `confidence`), and — only with `apply=true` or after UI confirmation — writes edges. Dry-run by default; edge types limited to RELATED_TO/DEPENDS_ON/PART_OF/SUPPORTS/CONTRADICTS/SUPERSEDES/EXAMPLE_OF. The graph UI exposes this as the "Suggest" tool with per-pair checkboxes (applied via `/api/graph/edges`).
- LLM-assisted consolidation (`suggest_consolidations`, `POST /api/graph/suggest-consolidations`): high-similarity pairs (default 0.45) are judged by the LLM as `merge` (duplicates — LLM picks the keeper, applied via `merge_graph_nodes`), `contradicts` (conflicting statements — CONTRADICTS edge), or `keep`. Dry-run by default; merges run before contradiction edges, and pairs touching a node merged away in the same pass are skipped.
- Temporal point-in-time search: `search_memory(..., as_of="2026-01-01")` (also `GET /api/memory/search?as_of=` and the MCP `memory_search` tool) returns only memory created on/before that moment — "what was true back then".
- Retrieval quality benchmark: `scripts/bench_memory.py` reports corpus stats (incl. an edge-quality audit: degree spread, isolated nodes, hub concentration), search latency, and relevance metrics (hit@1, hit@8, MRR, zero-result rate) against a copy of the live store plus synthetic scale tests. Default runs force offline hash embeddings; `--real-vectors` loads `./.env` and measures the production hybrid path against the persisted vectors (only bench queries hit the embedding API). `scripts/bench_gold_queries.json` holds a gold set of real user-style queries (cross-lingual, paraphrased) with expected label matches — a harder, more honest relevance signal than label self-retrieval.
- Graph-expanded context: `service.context()` (chat, council, MCP `memory_context`) calls `search_memory(..., expand_graph=True)`, which loads 1-hop neighbors of the matched nodes (`list_edges_touching`) so linked decisions/lessons join the packed context even without a lexical match; such hits are tagged with the `graph-neighbor` reason and counted in `retrieval.graph_neighbors`. Interactive search keeps expansion off for latency.
- API protection: per-start `X-ArchitectOS-Token` header plus loopback `Host`/`Origin` checks on every `/api/*` endpoint (see `docs/PRODUCTION.md`); bundle export strips OAuth tokens and `.env*` files never ship in release archives.
- E2E, fake adapter, release package, and startup/configuration tests.

## Future Options

- Native Electron/Tauri shell or signed platform installers.
- React/TypeScript migration if the static SPA outgrows dependency-free maintenance.
- Specialized ingestion templates for ADRs, issue trackers, commits, and meeting notes.
- Team sync/cloud backup after local-first behavior is stable.


See `docs/PRODUCTION.md` for production operations, readiness, backups, and security headers.
