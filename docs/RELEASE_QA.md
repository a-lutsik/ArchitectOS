# ArchitectOS Release QA

This checklist closes the initial ArchitectOS implementation plan and keeps the local package shippable without adding an installer framework yet.

## Build Package

```powershell
cd C:\git-views\ArchitectOS
python .\scripts\build_release.py
```

The script writes `dist\architectos-1.0.0.zip`. The archive excludes runtime state, local memory, SQLite data, cache files, and previous release archives.

For a non-writing preflight:

```powershell
python .\scripts\build_release.py --check-only
```

## Automated QA

```powershell
python -m unittest discover -s tests -v
python -m py_compile backend\app.py backend\architectos\models.py backend\architectos\search.py backend\architectos\server.py backend\architectos\service.py backend\architectos\storage.py backend\architectos\security.py backend\architectos\adapters.py backend\architectos\launcher.py backend\architectos\release.py scripts\build_release.py run_architectos.py
node --check frontend\app.js
```

Release tests verify:

- HTTP E2E app flow through the local server.
- Fake provider adapter route, stream, check, and model discovery.
- Startup script and configuration doc presence.
- Package manifest readiness and archive contents.
- Static desktop/tablet/mobile visual guardrails.

## Manual Visual QA

Desktop viewport:

- Workspace: context builder, open work, project files, selected file, and favorites are readable.
- Chat: provider selector, remember/approve controls, Stop button, and long assistant text do not overlap.
- Graph: canvas fills the stage, toolbar wraps cleanly, detail panel remains readable.
- Providers: cards, model controls, audit trail, and action badges fit.
- Settings: bundle and security preview textareas remain usable.

Mobile viewport:

- Sidebar collapses to a single-column top section with two-column nav buttons.
- Dashboard panels stack to one column.
- Task board becomes one column.
- Graph detail moves below the canvas.
- Long text wraps instead of widening the page.

## Release Notes

This package is a local-first Python/static-web app. Native installers, signed binaries, and Electron/Tauri shells are future packaging options rather than requirements for the current release.


See `docs/PRODUCTION.md` for production operations, readiness, backups, and security headers.
