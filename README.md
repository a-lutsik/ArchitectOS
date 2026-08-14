# ArchitectOS

ArchitectOS is a local-first AI workspace for software engineers. It turns project knowledge into persistent memory, builds AI-ready context packs, tracks work, and keeps provider configuration visible without storing secrets in memory.

## Run

```powershell
cd C:\git-views\ArchitectOS
python .\run_architectos.py
```

macOS/Linux:

```bash
python3 run_architectos.py
```

For an application-style window instead of a browser tab:

```powershell
.\start-architectos-app.ps1
```

The app-window launcher uses Edge/Chrome `--app` mode when available and falls back to the default browser if no compatible browser is found. It writes `data\architectos.runtime.json` while the server is alive. For no-browser development runs use `python .\backend\app.py`. See `docs\STARTUP.md` for port conflict handling and app-window startup, `docs\CONFIGURATION.md` for providers/security, `docs\PRODUCTION.md` for operations, and `docs\RELEASE_QA.md` for release packaging.

**Share Full package (OS login autostart):** `python scripts/build_share_package.py` → unpack → `install.sh` / `install.bat`. Russian guide for Full / MCP / IDE: [`docs/GUIDE_RU.md`](docs/GUIDE_RU.md). Installer overview: [`docs/INSTALLERS.md`](docs/INSTALLERS.md).

## Test

```powershell
python -m unittest discover -s tests -v
python -m py_compile backend\app.py backend\architectos\config.py backend\architectos\models.py backend\architectos\search.py backend\architectos\server.py backend\architectos\service.py backend\architectos\storage.py backend\architectos\security.py backend\architectos\adapters.py backend\architectos\routing.py backend\architectos\mcp.py backend\architectos\mcp_server.py backend\architectos\lsp.py backend\architectos\files.py backend\architectos\launcher.py backend\architectos\release.py scripts\build_release.py run_architectos.py mcp_memory_server.py
node --check frontend\app.js
python .\scripts\build_release.py --check-only
```

## Features

- Project registry with separate root folders and project-scoped local memory.
- UI language support for English, Russian, Ukrainian, and Hebrew with RTL layout for Hebrew.
- Scoped memory: interface, project, shared, global.
- Hybrid memory search with lexical score, local hash embeddings, scope priority, and graph expansion.
- Context Builder with relevant memory, project files, selected-file context, and enabled providers.
- Local chat/dialogs that respond from project memory.
- Structured workflows: Intro, Access, Rules, Help, Review.
- Task board with todo/doing/blocked/done states.
- Knowledge graph endpoint and visual graph cards.
- Graph Auto-Linker with root links, document/code edge typing, similarity links, and rebuild action.
- Provider catalog for Codex CLI, Claude Code, Gemini CLI, Ollama, OpenAI, Anthropic, and OpenRouter.
- Smart AI Router that auto-selects a provider by weighted cost, speed, quality, availability, and task role, with a routing preview.
- Multi-Agent team that runs one request across code, architecture, docs, and review roles and synthesizes a combined answer.
- MCP Hub with a real JSON-RPC stdio client and a registry for Filesystem, GitHub, Azure DevOps, Jira, Slack, Confluence, and Granola servers.
- MCP Memory Server (`mcp_memory_server.py`) that exposes the memory engine over MCP stdio so Cursor, GitHub Copilot, and Claude Desktop can search/read and add ArchitectOS memory. See `docs/MCP_MEMORY_SERVER.md`.
- Code Intelligence over the Language Server Protocol (Python, TypeScript, Go, Rust, Java) with document symbols and hover, no custom parsers.
- Project scan/import for a selected local project folder with per-project memory isolation.
- Extended memory ingestion for ADRs, issue notes, PR notes, meetings, semantic commit clusters, and duplicate hints.
- Human-like memory lifecycle with short-term freshness, access refresh, decay, archive/delete states, and long-term consolidation.
- Favorites for durable memory.
- Analytics for memory, tasks, chats, providers, scopes, and types.
- Profile backup/restore through `architectos.bundle` JSON.
- Security policy for prompt/result/audit redaction and no-secret persistence.
- Secret redaction before memory persistence.
- Desktop-style local launcher with browser tab mode, app-window mode, runtime state, and port conflict handling.
- E2E HTTP smoke tests, fake adapter tests, startup/configuration checks, release manifest, portable zip packaging, readiness checks, backups, and security headers.

## Architecture Patterns

- Repository: `SQLiteMemoryRepository` owns persistence and bundle serialization.
- Service/Facade: `ArchitectOSService` exposes app workflows to HTTP handlers.
- Launcher Facade: `architectos.launcher` owns local server lifecycle, port selection, browser launch, and app-window launch.
- Strategy: `HybridSearchStrategy` owns memory ranking and `RouterPolicy` (`architectos.routing`) owns weighted provider selection; both can be replaced later.
- Orchestrator: `CouncilOrchestrator` (`architectos.council`) fans one request out to role agents and synthesizes results.
- Adapter/Client: `MCPManager`/`MCPClient` (`architectos.mcp`) speak the MCP stdio JSON-RPC transport; `CodeIntelligenceManager`/`LSPClient` (`architectos.lsp`) speak the LSP stdio transport.
- Server (inbound MCP): `MemoryMCPServer` (`architectos.mcp_server`) serves the memory engine to external MCP clients over stdio, reusing `ArchitectOSService`.
