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

The backend is a stdlib-only Python 3.12 package under `backend/architectos`.
`ArchitectOSService` (`service.py`, ~213 lines) is a thin facade composed from 13
`*ServiceMixin` modules, one per concern: `ai_runtime_service`,
`settings_router_service`, `providers_council_service`, `integrations_service`,
`tool_exec_service`, `project_scan_service`, `azure_sync_service`,
`ingestion_service`, `chat_session_service`, `chat_service`, `retrieval_service`,
`graph_service`, and `lifecycle_service` (the newest). The facade `__init__` wires
the shared collaborators the mixins rely on: repository, embedding engine, provider
router, MCP/LSP managers, tool gateway, and the ingestion/lifecycle engines.

Provider adapters are layered so the two adapter families never import each other:
`adapters_base.py` holds `ProviderRequest`, `ProviderAdapter`, and `LocalMemoryAdapter`;
`adapters_cli.py` holds `CliAdapter` plus the subprocess and Gemini/Codex session
plumbing; `adapters.py` holds the HTTP families (OpenAI, Azure, Anthropic, OpenRouter,
Ollama), `ProviderRouter`, and re-exports the whole surface through `__all__` so
existing `from .adapters import X` call sites and `mock.patch` targets still resolve.
`_provider_urlopen` deliberately stays in `adapters.py` next to its callers, because
patching `adapters.urlopen` is how the tests intercept provider HTTP.

Agent tools follow the same pattern: `tool_ado.py` owns Azure DevOps MCP call
planning (`ado_call_plan` / `call_ado_tool`) shared with `azure_sync_service`;
`tool_format.py` owns result summarization and `tool_calls` parsing for the agent
loop; `tool_gateway.py` keeps `ToolSpec` catalogs, `ToolGateway`, and re-exports
the split surface through `__all__`.

Azure / Teams ingestion is composed the same way: `azure_sync_common.py` holds
the shared MCP/project helpers; `azure_boards_parse.py` owns Boards payload
normalization (HTML/ids/relations/comments); `azure_boards_sync.py`,
`azure_git_sync.py`, `azure_wiki_sync.py`, and `teams_sync.py` each own one
source domain; `azure_sync_service.py` is a thin facade mixin so
`ArchitectOSService` still lists a single `AzureSyncServiceMixin` in its MRO.

Project scanning follows the same pattern: `project_granola_ingest.py`,
`project_git_ingest.py`, and `project_files_service.py` own granola candidates,
local-git candidates, and project file I/O; `project_scan_service.py` keeps
scan/file/inbox/chat candidate builders and composes the mixins.
`memory_ingestion.py` owns `MemoryIngestionEngine` and token helpers;
`ingestion_timeouts.py` / `ingestion_rescan.py` / `ingestion_candidates.py`
own timeout budgets, background rescan, and list/promote/reject/batch;
`ingestion_service.py` keeps ingest/add-memory and re-exports the engine.
`graph_code_service.py` and `graph_suggest_service.py` own code-graph queries
and LLM link/consolidation suggestions; `graph_service.py` keeps the graph
view and edit commands. Schema migrations and bundled MCP/LSP/provider seed
catalogs live in `storage_defaults.py`; FTS/embeddings, candidate queue, and
tasks/chats/settings live in `storage_search.py`, `storage_candidates.py`, and
`storage_sessions.py` — `SQLiteMemoryRepository` composes those mixins.
`mcp_detect.py` owns Node/ADO remote detection; `adapters_extract.py` owns
HTTP response text helpers re-exported through `adapters.py`.

`server.py` keeps all HTTP routing in one place. A single `_dispatch` pipeline walks
the 106-entry `ROUTES` table (method + regex + handler, first match wins) and every
verb (`do_GET`/`do_POST`/`do_PATCH`) funnels through it: index shortcut, `/api/*`
authorization, query parsing, JSON body, route match, static-file fallthrough for
unmatched GETs. `_authorize_api` guards every `/api/*` call with the per-start
`X-ArchitectOS-Token` plus loopback `Host`/`Origin` checks; `_error` splits failures
into 400 for `ValueError` (bad input) and 500 for everything else.

User-controlled outbound URLs pass the SSRF guard in `netutil.py`:
`validate_outbound_url` requires http(s), classifies the host (resolving names via
DNS once), and rejects loopback, link-local, unspecified, reserved, and multicast
targets. Trusted endpoints can opt into loopback via `allow_local` (a per-config
flag or the `ARCHITECTOS_ALLOW_LOCAL_URLS` / `ARCHITECTOS_MCP_ALLOW_LOCAL` env
vars); link-local cloud metadata endpoints stay blocked even then. It is enforced
on provider `base_url` values in `adapters.py`, on embedding endpoints in
`embeddings.py` (where `is_loopback_url` derives the policy from the configured
endpoint, so a local inference server keeps working while a remote one stays
strict), and on remote MCP server URLs in
`mcp.py`.

The frontend under `frontend/` is dependency-free vanilla JS shipped as native ES
modules: `index.html` loads exactly one `<script type="module" src="/main.js">`
entry that imports every other module. `scripts/frontend_load_smoke.js` enforces
that contract without a browser: a single module entry with no leftover classic
script tags, a static import graph where every relative import resolves and every
named import matches a named export, no import cycles beyond the known verified
ones, and a Node import of the whole graph with browser globals stubbed. Long lists
(file tree, memory panel) render through `virtual-list.js`, a windowed renderer
that materializes only the rows intersecting the scroll viewport (plus overscan),
using two spacer divs so the scroll height matches the full row model.

## Storage

`data/architectos.db` is the source of truth. Memory evidence is also written under `memory/evidence` as markdown. Release packages exclude `data/`, `memory/`, `dist/`, and cache files.

`SQLiteMemoryRepository` (`storage.py`) owns the schema and core CRUD.
FTS5 / embedding search lives in `storage_search.py`
(`StorageSearchMixin`); the memory-candidate review queue lives in
`storage_candidates.py` (`StorageCandidatesMixin`); tasks/chats/providers/
settings/analytics/bundle I/O live in `storage_sessions.py`
(`StorageSessionsMixin`). Multi-step writes go
through `transaction()`: the connection is pinned to the creating thread via
thread-local storage (nested calls join the active transaction instead of opening a
second one), the unit of work opens with `BEGIN IMMEDIATE`, commits on clean exit,
and rolls back on exception. Schema versioning uses `PRAGMA user_version`:
`_migrate` applies every idempotent step in `MIGRATIONS` (defined in
`storage_defaults.py`) above the database's
stored version and stamps `SCHEMA_VERSION` (currently 3).

## Core Tables

- `projects`
- `memory_nodes`
- `memory_edges`
- `memory_candidates`
- `memory_embeddings`
- `tasks`
- `chat_sessions`
- `chat_context_summaries`
- `providers`
- `provider_runs`
- `settings`
- `keeper_events`
- `retrieval_feedback`

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
