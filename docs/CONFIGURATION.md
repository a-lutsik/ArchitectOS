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

Set provider keys in the shell that starts ArchitectOS. The values are never stored by the app; provider records store only the environment variable name.

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
- Codex CLI and Claude Code use explicit approval by default.

## No Secrets

ArchitectOS applies a shared security policy before prompt dispatch and before persistence. Prompts, provider results, stderr previews, task/chat payloads, settings, and exported bundles are redacted for common API key, token, bearer, private key, password, and basic-auth URL patterns.

Use Settings -> Security Preview or:

```powershell
$body = @{ text = "token=abc123456789xyz" } | ConvertTo-Json
Invoke-RestMethod -Uri http://127.0.0.1:8765/api/security/preview -Method Post -ContentType "application/json" -Body $body
```

## Verification

```powershell
python -m unittest discover -s tests -v
python -m py_compile backend\app.py backend\architectos\models.py backend\architectos\search.py backend\architectos\server.py backend\architectos\service.py backend\architectos\storage.py backend\architectos\security.py backend\architectos\adapters.py backend\architectos\launcher.py run_architectos.py
node --check frontend\app.js
```

## Troubleshooting

- If port `8765` is busy, use `python .\run_architectos.py --port 8765`; the launcher will pick the next available port.
- If you need a hard failure on port conflicts, add `--strict-port`.
- If a CLI provider returns `approval_required`, enable the chat approval checkbox or send `allow_cli: true` through the API.
- If provider tests show missing credentials, set the provider environment variable and restart ArchitectOS.


See `docs/PRODUCTION.md` for production operations, readiness, backups, and security headers.
