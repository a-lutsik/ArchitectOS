# ArchitectOS Roadmap Checklist

Use this as the shared implementation ledger. Statuses should match the in-app task board where practical.

## Done

- [x] Local-first Python HTTP app with SQLite persistence.
- [x] Scoped memory store, evidence markdown, hybrid search, and context builder.
- [x] Workspace UI for memory, context, tasks, chats, workflows, providers, analytics, settings, and bundle import/export.
- [x] MF0-1984-style interactive memory graph with filters, drag, focus, and detail panel.
- [x] Provider adapter layer for local-memory, OpenAI, Ollama, Codex CLI, and Claude CLI.
- [x] Real AI routing through `/api/ai/run`, `/api/chat/run`, and chat messages.
- [x] Provider setup test endpoint and Providers UI test button.
- [x] Provider setup hardening: richer status badges, setup hints, Test All, last-check persistence, and per-provider troubleshooting.
- [x] Streaming responses for API and CLI providers.
- [x] Codex and Claude CLI safety hardening: approvals, cancel, stderr UX, workdir policy, and audit trail.
- [x] Ollama model discovery picker and local daemon guidance.
- [x] Anthropic and OpenRouter adapters.
- [x] Real multi-step workflow engine: scan, build context, ask provider, write task/memory, review.
- [x] Memory ingestion and promotion flow from docs, code, git history, and chat.
- [x] Extended ingestion templates for ADRs, issues, PRs, meetings, semantic commit clusters, and duplicate hints.
- [x] Human-like memory lifecycle: short-term/long-term tiers, refresh-on-use, decay, archive/delete states, and configurable consolidation.
- [x] Graph actions: open node, pin, merge, create edge, explain path, and filter by task/provider.
- [x] Graph Auto-Linker: scanned/promoted/existing nodes receive root, typed, and similarity edges with a rebuild action.
- [x] Project workspace integration: open files, git diff, selected-file context builder.
- [x] Desktop shell packaging with local server lifecycle and port conflict handling.
- [x] App-window launcher mode through Edge/Chrome `--app`, with PowerShell/BAT helpers.
- [x] Security layer: explicit approvals, prompt/result redaction, and no secret persistence.
- [x] E2E tests, fake adapter tests, installer/start script, and configuration docs.
- [x] Release packaging and visual QA across desktop and mobile.
- [x] Production hardening: readiness, version metadata, backups, security headers, and operational docs.
- [x] Smart AI Router: weighted cost/speed/quality/availability scoring, task-role classification, strategies, and routing preview.
- [x] Multi-Agent system: code/architecture/docs/review role agents with synthesis, reusing routing and audit.
- [x] MCP Hub: real MCP stdio JSON-RPC client, registry for Filesystem/GitHub/Azure DevOps/Jira/Slack/Confluence/Granola, test/list-tools/call with approval gating.
- [x] MCP Memory Server: expose the memory engine over MCP stdio (`mcp_memory_server.py`) for Cursor/Copilot/Claude with search/context/add/list-projects tools.
- [x] Code Intelligence: real LSP stdio JSON-RPC client for Python/TypeScript/Go/Rust/Java with document symbols and hover.

## In Progress

- [ ] None. Production baseline is complete.

## Future Options

- [ ] Native Electron/Tauri shell or signed platform installers.
- [ ] React/TypeScript migration if the static SPA outgrows dependency-free maintenance.
- [ ] Persist MCP tool-call results and LSP symbols into project memory automatically.
- [ ] Streaming multi-agent runs and per-role cost/token analytics.
