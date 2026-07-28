# ArchitectOS Production Operations

ArchitectOS is production-ready as a local-first desktop/server package. It is not a multi-tenant hosted service; production means reproducible local startup, app-window launch, operational health checks, backups, security headers, release packaging, and deterministic verification.

## Version

Current production version: `1.0.0`.

```powershell
python .\run_architectos.py
```

Application-style window:

```powershell
.\start-architectos-app.ps1
```

Useful environment variables:

- `ARCHITECTOS_HOST`: bind host, defaults to `127.0.0.1`.
- `ARCHITECTOS_PORT`: preferred port, defaults to `8765`.
- `ARCHITECTOS_ENV`: label returned by `/api/version`, defaults to `local`.
- `ARCHITECTOS_BACKUP_RETENTION`: local backup retention count, defaults to `10`.
- `ARCHITECTOS_ACCESS_LOG`: set to `1` for request logs.

## Health And Readiness

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/health
Invoke-RestMethod http://127.0.0.1:8765/api/ready
Invoke-RestMethod http://127.0.0.1:8765/api/version
```

Readiness validates the SQLite database, required frontend assets, security settings, release manifest, and provider catalog.

## Backups

Create a local SQLite backup:

```powershell
Invoke-RestMethod -Uri http://127.0.0.1:8765/api/ops/backup -Method Post -ContentType "application/json" -Body "{}"
```

List backups:

```powershell
Invoke-RestMethod http://127.0.0.1:8765/api/ops/backups
```

Backups are written under `backups/` and are excluded from release archives.

## API Authentication

Every `/api/*` request (except the MCP OAuth callback) requires the per-start header `X-ArchitectOS-Token`. The token is generated on each server start, injected into the served `index.html` for the SPA, and written to `data/architectos.runtime.json` (mode `0600`) alongside the port for local tooling:

```powershell
$state = Get-Content data\architectos.runtime.json | ConvertFrom-Json
Invoke-RestMethod -Uri "$($state.url)/api/health" -Headers @{ "X-ArchitectOS-Token" = $state.auth_token }
```

Requests with a foreign `Host` header (DNS rebinding) or a cross-site `Origin` are rejected with `403`, so a malicious website cannot drive the local API while the app is running. Do not set `ARCHITECTOS_HOST` to a non-loopback address — the token is the only credential, and the threat model assumes loopback-only binding.

## Security Headers

The local HTTP server sends defensive defaults:

- `X-Content-Type-Options: nosniff`
- `X-Frame-Options: DENY`
- `Referrer-Policy: no-referrer`
- `Content-Security-Policy` scoped to local self resources
- `Cache-Control: no-store` for API responses

## Release

```powershell
python .\scripts\build_release.py --check-only
python .\scripts\build_release.py
```

The release manifest must report `ready: true` before shipping. The archive includes both browser-tab and app-window launch scripts, and excludes runtime state, local memory evidence, SQLite data, backups, caches, previous release archives, and `.env*` secret files.
