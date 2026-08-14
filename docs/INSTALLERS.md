# ArchitectOS Installers (Full / MCP / IDE)

Three separate installers ship ArchitectOS for different hosts. First wave:
**macOS + Windows**. Linux and VS Code are out of scope for v1. Code signing /
notarization is a follow-up (unsigned macOS builds need Gatekeeper right-click
→ Open).

```text
dist/
  desktop/ArchitectOS_<ver>_macos.dmg
  desktop/ArchitectOS_<ver>_windows.msi   # or NSIS .exe
  mcp/architectos-mcp_<ver>_macos.zip
  mcp/architectos-mcp_<ver>_windows.zip
  ide/ArchitectOS-Memory-<ver>.zip
  share/ArchitectOS_Full_<ver>_<platform>.zip   # portable Full + OS autostart
```

Orchestration:

```bash
make dist-desktop   # or: python scripts/build_all_installers.py --target desktop
make dist-mcp
make dist-ide
make dist-share     # Full zip with install.sh / install.bat (login autostart)
make dist-share-all # same, and embed MCP/IDE artifacts if already in dist/
make dist-all
```

On Windows use the `.ps1` twins (`scripts/build_desktop.ps1`,
`scripts/build_mcp.ps1`, `scripts/build_ide_plugin.ps1`) or
`python scripts/build_all_installers.py` /
`python scripts/build_share_package.py`.

Default PR CI does **not** run Tauri/PyInstaller builds.

Russian end-user guide (Full / MCP / IDE): [GUIDE_RU.md](GUIDE_RU.md).

---

## Shared data directory (`ARCHITECTOS_ROOT`)

Full Desktop and the MCP binary share memory when they use the same root
(directory that contains `data/architectos.db` and, while the server is up,
`data/architectos.runtime.json`).

| Context | Default root |
| --- | --- |
| Source checkout (`python run_architectos.py`) | Repository root |
| Packaged desktop / MCP binary | macOS/Linux: `~/ArchitectOS` · Windows: `%LOCALAPPDATA%\ArchitectOS` |
| Override | Set env `ARCHITECTOS_ROOT` to an absolute path |

The IntelliJ plugin talks HTTP to a running server; use **Detect** to read
`data/architectos.runtime.json` for URL + `auth_token`.

---

## 1. Full desktop (Tauri 2)

**What it is:** a native window (`desktop/`) whose webview loads the existing
SPA from `http://127.0.0.1:<port>/`. On start the shell spawns the
`architectos-server` sidecar (PyInstaller bundle of the Python HTTP server +
`frontend/`); on quit it kills that child. Runtime port/token are written to
`data/architectos.runtime.json` (same as `run_architectos.py`).

### Prerequisites

| Tool | Why |
| --- | --- |
| Rust (`rustup` → `cargo`, `rustc`) | Tauri shell |
| Node.js 20+ + npm | `@tauri-apps/cli` in `desktop/` |
| Python 3.12+ | Sidecar source / PyInstaller |
| PyInstaller ≥ 6 | `pip install 'pyinstaller>=6.0'` |
| macOS: Xcode CLT · Windows: MSVC Build Tools + WebView2 | Platform SDKs |

Scripts fail fast with a clear message if Rust, Node, Python, or PyInstaller
are missing.

### Build

```bash
./scripts/build_desktop.sh          # macOS / Linux
# .\scripts\build_desktop.ps1       # Windows
```

Pipeline: PyInstaller (`architectos-server.spec`) → copy sidecar into
`desktop/src-tauri/binaries/architectos-server-<target-triple>` →
`npm run build` (Tauri) → copy `.dmg` / `.msi` / `.exe` into `dist/desktop/`.

### Dev without a full bundle

From a checkout with Python:

```bash
cd desktop && npm install && npm run tauri dev
```

If the sidecar binary is absent, the Rust shell falls back to
`python3 run_architectos.py --no-browser` and uses the repo as
`ARCHITECTOS_ROOT` unless you override it.

Optional env:

- `ARCHITECTOS_ROOT` — data directory
- `ARCHITECTOS_PORT` — preferred port (default `8765`)
- `ARCHITECTOS_SERVER_BIN` — path to a prebuilt sidecar

### Out of scope (v1)

Code signing / notarization, Linux packages.

---

## 2. MCP memory server (PyInstaller CLI)

**What it is:** a **stdio** MCP executable (`architectos-mcp`) for Cursor,
Claude, Copilot, etc. It opens `data/architectos.db` directly — the desktop app
does **not** need to be running. This is intentionally **not** Tauri.

### Prerequisites

- Python 3.12+
- PyInstaller ≥ 6 (`pip install 'pyinstaller>=6.0'`)

### Build

```bash
./scripts/build_mcp.sh
# .\scripts\build_mcp.ps1
```

Produces under `dist/mcp/`:

- `architectos-mcp` / `architectos-mcp.exe`
- `README-mcp.txt` (Cursor / Claude config snippets)
- `architectos-mcp_<ver>_<platform>.zip`

Point MCP clients at the binary and set `ARCHITECTOS_ROOT` to the shared data
root (see above). Full protocol docs: [MCP_MEMORY_SERVER.md](MCP_MEMORY_SERVER.md).

### Out of scope (v1)

GUI installer, auto-merge into `~/.cursor/mcp.json` (snippets are documented;
optional helpers may land later).

---

## 3. IntelliJ IDE plugin

**What it is:** the thin HTTP client under `ide/intellij` (search / add /
feedback / Detect). Needs a running ArchitectOS HTTP server (full desktop app
**or** `python run_architectos.py`).

### Prerequisites

- JDK 17+
- Gradle wrapper in `ide/intellij/` (`./gradlew`)

### Build

```bash
./scripts/build_ide_plugin.sh
# .\scripts\build_ide_plugin.ps1
```

Copies `ArchitectOS-Memory-<version>.zip` to `dist/ide/`.

### Install

Settings → Plugins → ⚙ → **Install Plugin from Disk** → select the zip.

Settings → Tools → ArchitectOS Memory → **Detect** (reads
`architectos.runtime.json`).

Plugin README: [ide/intellij/README.md](../ide/intellij/README.md).

### Out of scope (v1)

JetBrains Marketplace publish, VS Code extension.

---

## 4. Full share package (portable zip + OS autostart)

**What it is:** a zip you can send to a teammate. After unpack they run
`install.sh` (macOS) or `install.bat` (Windows). That copies `architectos-server`
into `ARCHITECTOS_ROOT/bin` and registers **login/boot autostart** so the HTTP
server comes up without opening the Tauri window.

| OS | Autostart mechanism |
| --- | --- |
| macOS | LaunchAgent `com.architectos.server` (`RunAtLoad` + `KeepAlive`) |
| Windows | Scheduled Task «ArchitectOS Server» at logon + `HKCU\...\Run` fallback |
| Linux (bonus) | systemd user unit when `systemctl --user` is available |

### Build

```bash
python3 scripts/build_share_package.py
# or: make dist-share
# optional: --with-mcp --with-ide  /  make dist-share-all
```

Requires PyInstaller (same as the desktop sidecar). Output:
`dist/share/ArchitectOS_Full_<ver>_<platform>.zip`.

### Recipient steps

1. Unpack the zip.
2. Run `./install.sh` or `install.bat`.
3. Open `http://127.0.0.1:8765/`.
4. To disable: `./uninstall.sh` / `uninstall.bat` (data under `ARCHITECTOS_ROOT` is kept).

Repo helpers (without a zip): `scripts/install_autostart.sh` /
`scripts/install_autostart.ps1` (point `ARCHITECTOS_SERVER_BIN` at a built sidecar).

### Out of scope (v1)

Code signing, GUI installer wizard, elevating to a system-wide Windows service.

---

## Smoke checklist (manual)

Run after building the artifacts you care about.

### Full desktop

- [ ] Installer opens without a Python install on PATH (sidecar embedded).
- [ ] Window shows the ArchitectOS UI (not a blank webview).
- [ ] Quit kills the sidecar (`architectos.runtime.json` removed; port free).
- [ ] With MCP using the same `ARCHITECTOS_ROOT`, memory search sees desktop data.

### MCP

- [ ] `echo '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | ./architectos-mcp` returns tools.
- [ ] Cursor (or Claude) lists `architectos-memory` tools after config reload.
- [ ] `memory_search` / `memory_add` hit the expected `ARCHITECTOS_ROOT` DB.

### IDE plugin

- [ ] Install from Disk succeeds in IntelliJ IDEA.
- [ ] With the server running, **Detect** fills URL + token.
- [ ] Search returns hits; Add Selection writes memory.

### Full share + autostart

- [ ] After `install`, server answers on `http://127.0.0.1:8765/` without Python on PATH.
- [ ] Log off / reboot (or `launchctl` / Task Scheduler start) brings the server back.
- [ ] `uninstall` removes LaunchAgent / Scheduled Task; `architectos.runtime.json` cleared after stop.
- [ ] MCP / IDE using the same `ARCHITECTOS_ROOT` see the same memory.

---

## Related docs

- [GUIDE_RU.md](GUIDE_RU.md) — Russian user guide (Full / MCP / IDE)
- [STARTUP.md](STARTUP.md) — port selection, app-window launcher
- [CONFIGURATION.md](CONFIGURATION.md) — providers / security
- [MCP_MEMORY_SERVER.md](MCP_MEMORY_SERVER.md) — MCP tools and client snippets
- [ROADMAP_CHECKLIST.md](ROADMAP_CHECKLIST.md) — release-train status
