# ArchitectOS Configuration

ArchitectOS is intentionally dependency-light: Python serves the API and static frontend, SQLite stores local state, and provider credentials stay in environment variables.

## Paths

- `backend/app.py` starts the fixed-port development server.
- `run_architectos.py` starts the desktop-style launcher.
- `frontend/` contains the static UI.
- `data/architectos.db` stores projects, memory, tasks, chats, provider settings, and audit records.
- `memory/evidence/` stores markdown evidence generated for durable memory nodes.
- `data/architectos.runtime.json` exists only while the launcher is running.

## Provider Environment

Set provider and integration keys in the UI after install: **Setup → Providers** for model keys, **Settings → Connections** for Azure DevOps. The app writes them to `.env.local` and never stores secrets in SQLite. A process environment or `.env` file still works as an override.

```powershell
$env:OPENAI_API_KEY = "..."
$env:ANTHROPIC_API_KEY = "..."
$env:OPENROUTER_API_KEY = "..."
$env:OLLAMA_BASE_URL = "http://127.0.0.1:11434"
python .\run_architectos.py
```

Provider settings:

- OpenAI defaults to `OPENAI_API_KEY`.
- Anthropic defaults to `ANTHROPIC_API_KEY`.
- OpenRouter defaults to `OPENROUTER_API_KEY`.
- Ollama defaults to `http://127.0.0.1:11434`.
- Codex CLI and Claude Code use explicit approval by default. Without approval, auto routing skips them and answers through an API provider instead of stalling the turn.

## Ask and MCP memory

Ask messages stay in the dialog history while the session is active. ArchitectOS does **not** create an Ask memory candidate after every turn.

An Ask session ends when:

- you click **End** on the dialog card (or End session in Ask), or
- the chat is idle for `memory_lifecycle.chat_session_idle_minutes` (default `30`; `0` disables idle finalize).

On finalize, ArchitectOS extracts durable facts into the review queue (`template=chat_session_atom`, label `Chat fact:`). A session summary is stored in `chat_context_summaries` for chat packing and the next session's LLM prompt; a `chat_session_summary` review card is written only when no queueable atoms were extracted (digest fallback). If those facts are already queued as MCP, Ask finalize skips the queue entirely. Continuing the same dialog reopens the session and bumps `session_revision`. Explicit **Remember answer** and ★ favorite still bypass the session gate.

MCP `memory_turn` and agent hooks use the same capture path (`capture_memory_turn`) but a different channel:

| Channel | Source in the UI | `source_type` | Queue label |
| --- | --- | --- | --- |
| Ask | Ask view / Workspace Ask | `chat` | **Ask** chip, `Chat fact:` |
| MCP | `memory_turn` or an agent hook | `mcp` | **MCP** chip, `MCP fact:` |

The same durable fact is one card. The first channel wins; Ask does not recast an MCP fact (and the reverse). Completed Ask threads are not dumped again by AutoScan.

Chat-memory mode (`off` / `strict` / `aggressive`) lives under **Settings → Chat memory**. Agent-hook install is **Setup → Agent hooks**; see `docs/AGENT_HOOKS.md` and `docs/MCP_MEMORY_SERVER.md`.

## No Secrets

ArchitectOS applies a shared security policy before prompt dispatch and before persistence. Prompts, provider results, stderr previews, task/chat payloads, settings, and exported bundles are redacted for common API key, token, bearer, private key, password, and basic-auth URL patterns.

Use Settings -> Security Preview or:

```powershell
$body = @{ text = "token=abc123456789xyz" } | ConvertTo-Json
Invoke-RestMethod -Uri http://127.0.0.1:8766/api/security/preview -Method Post -ContentType "application/json" -Body $body
```

## Verification

```powershell
python -m unittest discover -s tests -v
python -m py_compile backend\app.py backend\architectos\models.py backend\architectos\search.py backend\architectos\server.py backend\architectos\service.py backend\architectos\storage.py backend\architectos\security.py backend\architectos\adapters.py backend\architectos\launcher.py run_architectos.py
node --check frontend\app.js
```

## Troubleshooting

- If port `8766` is busy, use `python .\run_architectos.py --port 8766`; the launcher will pick the next available port.
- If you need a hard failure on port conflicts, add `--strict-port`.
- If a CLI provider you selected explicitly returns `approval_required`, use the "Enable CLI runs" button in the reply or send `allow_cli: true` through the API. Reading memory and project files never needs approval.
- If provider tests show missing credentials, paste the key in Setup → Providers and Test. Restart is not required.


See `docs/PRODUCTION.md` for production operations, readiness, backups, and security headers.
