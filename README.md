# ArchitectOS

**ArchitectOS** is a local-first AI workspace for software engineers. It turns
project knowledge into persistent memory, builds AI-ready context packs for your
coding agents, tracks work on a task board, visualizes a knowledge graph, and
keeps provider configuration visible — without ever storing secrets in the
database.

Everything runs on your machine: a Python HTTP server serves a static web UI and
stores data in a local SQLite database. Nothing is uploaded unless you call an
AI provider you configured.

> 🌐 Документация на русском: [docs/GUIDE_RU.md](docs/GUIDE_RU.md) (гайд по
> установке Full / MCP / IDE) и [docs/PROJECT_RU.md](docs/PROJECT_RU.md)
> (полное описание проекта и настройка).

---

## Highlights

- **Persistent project memory** — scoped (interface / project / shared /
  global) with hybrid lexical + embedding search and a knowledge graph
  (2D Map / 3D Galaxy).
- **Context Builder** — packs the most relevant memory, project files and
  selected-file context into a token budget for your agent.
- **Smart AI Router** — auto-selects a provider (OpenAI, Anthropic, OpenRouter,
  Azure OpenAI, Gemini, Ollama, CLI agents) by cost, speed, quality and task
  role; multi-agent **Council** can fan one request across roles.
- **MCP hub + memory server** — a real stdio MCP client and an
  `architectos-mcp` memory server so Cursor, Claude, and Copilot can read and
  add the same memory.
- **Agent hooks** — capture turns from Cursor / Claude Code / Codex and inject
  per-question memory even when the agent never calls a memory tool.
- **Structured workflows** (Intro / Access / Rules / Help / Review), task board,
  LSP code intelligence, project import/scan, memory lifecycle (decay,
  consolidation, archive), analytics, profile backup.
- **No secrets in the DB** — values are redacted before persistence and exports.

See [docs/PROJECT_RU.md](docs/PROJECT_RU.md) for the full component list.

---

## Repository layout

| Path | Purpose |
| --- | --- |
| `backend/` | Python HTTP server + `architectos` package (the whole engine). Runtime is **stdlib-only** Python 3.12+. |
| `frontend/` | Static single-page UI served by the server. |
| `run_architectos.py` | Desktop-style launcher (browser tab / app window / no-browser). |
| `start-architectos.*` | Shell/batch helpers to start the launcher. |
| `desktop/` | Optional Tauri 2 native shell (`architectos-server` sidecar). |
| `ide/intellij/` | Optional IntelliJ plugin that talks HTTP to a running server. |
| `packaging/` · `scripts/` | Release/installer/autostart tooling. |
| `docs/` | Full documentation (see [Documentation](#documentation)). |
| `tests/` | Unit, integration and E2E smoke tests. |
| `.github/workflows/ci.yml` | CI: lint + mypy + tests + coverage on macOS / Windows / Linux. |

---

## Requirements

| OS | Requirements |
| --- | --- |
| macOS 12+ / Linux | **Python 3.12+** (`python3`). No other runtime needed to run the core app. |
| Windows 10/11 | **Python 3.12+** (`python`) — install from [python.org](https://www.python.org) and tick *Add to PATH*. |
| All | Chrome or Edge if you want the **app-window** launcher (`--app-window`). Otherwise the default browser is used. |

Optional, per feature:

| Feature | Extra requirement |
| --- | --- |
| Real vector embeddings | `pip install -e ".[vectors,embeddings]"` (fastembed/ONNX; no API key needed) or an embedder API key. |
| Packaging installers (`dist/*`) | PyInstaller ≥ 6 (`pip install -e ".[packaging]"`). |
| Desktop shell | Rust toolchain + Node 20+ (see [docs/INSTALLERS.md](docs/INSTALLERS.md)). |
| Frontend syntax checks / CI | Node 20+. |

---

## Quick start (run from the source)

### 1. Clone

The repository is **private** — you need read access from the owner.

```bash
git clone git@github.com:a-lutsik/ArchitectOS.git
# or HTTPS:
git clone https://github.com/a-lutsik/ArchitectOS.git
cd ArchitectOS
```

### 2. (Optional) virtual environment + extras

The core app is stdlib-only, so this step is **not required** to run. Install it
only if you want vector embeddings and other extras in an isolated env:

```bash
# macOS / Linux
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[vectors,embeddings]"
```

```powershell
# Windows (PowerShell)
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[vectors,embeddings]"
```

### 3. Configure providers (optional)

ArchitectOS works offline (memory, tasks, graph) without any key. To enable AI
Ask/Chat answers, add at least one provider — see
[Connect AI providers](#connect-ai-providers-api-keys) below.

### 4. Run

```bash
# macOS / Linux
python3 run_architectos.py
```

```powershell
# Windows (PowerShell)
python run_architectos.py
# or the helper scripts:
.\start-architectos.ps1          # open in the default browser
.\start-architectos-app.ps1      # open in an Edge/Chrome app window
```

Then open <http://127.0.0.1:8766/>.

Useful launcher flags ([docs/STARTUP.md](docs/STARTUP.md)):

```bash
python3 run_architectos.py --app-window      # desktop-app feel (Chrome/Edge app mode)
python3 run_architectos.py --no-browser      # headless server (good for autostart)
python3 run_architectos.py --port 8766       # preferred port; next free port is used if busy
python3 run_architectos.py --port 8766 --strict-port   # fail instead of switching ports
```

On first run a fresh local database is created at `data/architectos.db`
(`data/` is gitignored and ships empty in this repository).

---

## Connect AI providers (API keys)

Provider keys live only in your local environment — never in git or in SQLite.

The server reads, in order (first found wins):

1. `.env.local` in the repo root — **created automatically** when you save a key
   in the UI under **Setup → Providers**;
2. `.env` in the repo root — create it from the committed template:

```bash
cp .env.example .env      # macOS / Linux
Copy-Item .env.example .env   # Windows (PowerShell)
```

Then paste the keys you need into `.env`. Common variables (full list in
[`.env.example`](.env.example) and [docs/CONFIGURATION.md](docs/CONFIGURATION.md)):

| Provider | Environment variable | Where to get it |
| --- | --- | --- |
| OpenAI | `OPENAI_API_KEY` | platform.openai.com |
| Anthropic | `ANTHROPIC_API_KEY` | console.anthropic.com |
| OpenRouter | `OPENROUTER_API_KEY` | openrouter.ai/keys |
| Google Gemini | `GEMINI_API_KEY` or `GOOGLE_API_KEY` | aistudio.google.com |
| Azure OpenAI | `AZURE_OPENAI_API_KEY` + `AZURE_OPENAI_ENDPOINT` + `AZURE_OPENAI_DEPLOYMENT` | Azure portal |
| Ollama (local) | `OLLAMA_BASE_URL` (default `http://127.0.0.1:11434`) | — |

Recommended flow: start the app → **Setup → Providers** → pick a provider →
paste the key → **Test**. The UI stores it in `.env.local` (mode
`0600`/restricted, gitignored).

CLI agents (Claude Code, Codex, Gemini CLI) are detected from your local CLI
sessions; enable them explicitly in the UI (explicit approval is the default).

Integration keys (optional): GitHub/GitLab MCP tokens, Azure DevOps
`ADO_ORG`/`ADO_MCP_AUTH_TOKEN`, and Microsoft Graph/Teams variables are described
in [`.env.example`](.env.example).

---

## Start at OS login (autostart)

Two routes — pick the one that fits.

### Route A — source checkout (recommended for developers)

Add a one-line autostart that runs the headless server
(`python3 run_architectos.py --no-browser`) at login.

**macOS** — create `~/Library/LaunchAgents/com.architectos.server.plist`
(adjust `<PATH>/ArchitectOS` to your checkout and the interpreter path):

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.architectos.server</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/you/ArchitectOS/run_architectos.py</string>
    <string>--no-browser</string>
    <string>--port</string><string>8766</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.architectos.server.plist
# disable later: launchctl unload ~/Library/LaunchAgents/com.architectos.server.plist
```

**Linux (systemd, user session)** — `~/.config/systemd/user/architectos-server.service`:

```ini
[Unit]
Description=ArchitectOS server

[Service]
ExecStart=/usr/bin/python3 /home/you/ArchitectOS/run_architectos.py --no-browser --port 8766
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now architectos-server
```

**Windows** — register a Scheduled Task at logon (PowerShell):

```powershell
$action  = New-ScheduledTaskAction -Execute "python.exe" `
  -Argument "C:\ArchitectOS\run_architectos.py --no-browser --port 8766"
$trigger = New-ScheduledTaskTrigger -AtLogOn
Register-ScheduledTask -TaskName "ArchitectOS Server" -Action $action -Trigger $trigger
# disable later: Unregister-ScheduledTask -TaskName "ArchitectOS Server"
```

> A shared data root can be set with the `ARCHITECTOS_ROOT` environment variable
> so the server, MCP binary and desktop app all use the same `data/`.

### Route B — packaged Full share package

Build a portable zip with a real `architectos-server` binary and an installer
that registers **login/boot autostart** (macOS LaunchAgent / Windows Scheduled
Task / Linux systemd):

```bash
python3 scripts/build_share_package.py
# unpack dist/share/ArchitectOS_Full_*.zip on the target machine, then:
#   macOS/Linux: ./install.sh
#   Windows:     install.bat
```

The installer copies the binary into `ARCHITECTOS_ROOT/bin`, probes the vector
runtime, and registers autostart. Disable with `./uninstall.sh` /
`uninstall.bat`. Details: [docs/INSTALLERS.md](docs/INSTALLERS.md).

---

## Documentation

All documentation lives in [`docs/`](docs):

| Doc | Content |
| --- | --- |
| [STARTUP.md](docs/STARTUP.md) | Startup, app-window launcher, port handling, runtime state. |
| [CONFIGURATION.md](docs/CONFIGURATION.md) | Paths, provider env vars, memory modes, security. |
| [PRODUCTION.md](docs/PRODUCTION.md) | Operations: readiness, backups, security headers. |
| [AGENT_HOOKS.md](docs/AGENT_HOOKS.md) | Agent turn capture for Cursor / Claude Code / Codex. |
| [MCP_MEMORY_SERVER.md](docs/MCP_MEMORY_SERVER.md) | `architectos-mcp` protocol and client setup. |
| [SDK.md](docs/SDK.md) | Python SDK (`from architectos import ArchitectOS`). |
| [INSTALLERS.md](docs/INSTALLERS.md) | Desktop / MCP / IDE / share-package installers. |
| [RELEASE_QA.md](docs/RELEASE_QA.md) | Release packaging and QA checklist. |
| [ARCHITECTURE.md](docs/ARCHITECTURE.md) · [CODE_GRAPH.md](docs/CODE_GRAPH.md) | Architecture and code-map. |
| [SEARCH_MEMORY_GUIDE.md](docs/SEARCH_MEMORY_GUIDE.md) | Searching and managing memory. |
| [GUIDE_RU.md](docs/GUIDE_RU.md) · [PROJECT_RU.md](docs/PROJECT_RU.md) | Russian user guide & project documentation. |

---

## Tests

Run the full Python suite (macOS / Linux):

```bash
python3 -m unittest discover -s tests -v
```

Or use the CI commands that also run on GitHub Actions
(macOS + Windows + Ubuntu in [`.github/workflows/ci.yml`](.github/workflows/ci.yml)):

```bash
python3 -m ruff check .
python3 -m mypy
MEMORY_EMBEDDING_PROVIDER=hash python3 -m coverage run -m unittest discover -s tests -p 'test_*.py'
python3 -m coverage report --fail-under=72
```

---

## Data, privacy & security

- **Local-first.** The SQLite DB (`data/architectos.db`), memory evidence and
  backups stay on your machine. This repository ships with **no user data** —
  `data/`, `memory/`, `backups/`, `.env*` are gitignored.
- **Secrets are not stored in the DB.** Provider keys are kept in
  `.env.local` / `.env` (gitignored). A shared security policy redacts API keys,
  tokens, bearer headers, private keys, passwords and basic-auth URLs before
  prompts are dispatched, results are shown, or memory is persisted.
  Preview it in the UI under **Settings → Security preview**.
- **Local server only.** The API binds to `127.0.0.1` and every `/api/*`
  request requires a per-run auth token injected into the page, protecting
  against CSRF/DNS-rebinding. See [docs/CONFIGURATION.md](docs/CONFIGURATION.md)
  and [docs/PRODUCTION.md](docs/PRODUCTION.md).

---

## Repository hygiene (for maintainers)

- Never commit credentials, API keys, `.env*` (except the committed
  `.env.example` template), `data/`, `memory/`, `backups/`, or local tool
  directories (`.claude/`, `.impeccable/`, `.cursor/` choices, `.DS_Store`).
- The data root (`ARCHITECTOS_ROOT`) defaults to the checkout when run from
  source — keep `data/` out of commits (already handled by `.gitignore`).
