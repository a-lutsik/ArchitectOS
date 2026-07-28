# ArchitectOS Startup

ArchitectOS runs as a local Python HTTP app with a desktop-style launcher. The launcher starts the API and frontend together, opens the browser by default, and records a small runtime state file while the server is alive.

## Start

```powershell
cd C:\git-views\ArchitectOS
python .\run_architectos.py
```

Equivalent shell helpers:

```powershell
.\start-architectos.ps1
```

```cmd
start-architectos.bat
```

## Application Window

Use app-window mode when ArchitectOS should feel like a desktop application instead of a web page:

```powershell
.\start-architectos-app.ps1
```

```cmd
start-architectos-app.bat
```

Equivalent direct command:

```powershell
python .\run_architectos.py --app-window
```

The launcher looks for Edge or Chrome-compatible browsers and opens `--app=http://...`, which creates a separate app-style window without tabs or an address bar. If no compatible browser is found, ArchitectOS falls back to the default browser.

## Port Handling

The preferred port is `8765`. If it is busy, the launcher picks the next available port and prints the actual URL.

```powershell
python .\run_architectos.py --port 8765
```

Use strict mode when another process on the preferred port should be treated as an error:

```powershell
python .\run_architectos.py --port 8765 --strict-port
```

## Runtime State

While the server is running, ArchitectOS writes:

```text
data\architectos.runtime.json
```

The file contains the active host, port, URL, process id, launch mode, and timestamps. It is removed when the launcher shuts down cleanly.

## Developer Server

For test/dev runs that should not open a browser:

```powershell
python .\backend\app.py
```

The lower-level dev server keeps the historical fixed-port behavior. Use the launcher when you want port conflict handling.

## Installer Checks

The repository includes dependency-free startup checks in `tests/test_e2e.py`. They verify that `run_architectos.py --help`, browser startup scripts, app-window startup scripts, package scripts, and configuration docs stay in sync.

## Release Package

Build a portable zip package with:

```powershell
python .\scripts\build_release.py
```

The archive is written under `dist\` and excludes runtime data, local memory evidence, cache files, and previous release packages.

See `docs/PRODUCTION.md` for production operations, readiness, backups, and security headers.
