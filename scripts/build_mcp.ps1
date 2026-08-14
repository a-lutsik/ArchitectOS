# Build the standalone ArchitectOS MCP memory server (PyInstaller one-file).
# Output: dist/mcp/architectos-mcp_<version>_windows.zip (+ binary + README-mcp.txt)
$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

function Test-PyInstaller {
    if (Get-Command pyinstaller -ErrorAction SilentlyContinue) { return $true }
    try {
        & python -c "import PyInstaller" 2>$null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

if (-not (Test-PyInstaller)) {
    Write-Error @"
PyInstaller is required to build architectos-mcp.
  pip install 'pyinstaller>=6.0'
Then re-run: $($MyInvocation.MyCommand.Path)
"@
    exit 1
}

$Version = & python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])"
if ($LASTEXITCODE -ne 0 -or [string]::IsNullOrWhiteSpace($Version)) {
    Write-Error "Could not read project version from pyproject.toml"
    exit 1
}
$Version = $Version.Trim()
$Platform = "windows"
$OutDir = Join-Path $Root "dist\mcp"
$WorkDir = Join-Path $Root "build\mcp"
$StageDir = Join-Path $OutDir "stage"
$ZipName = "architectos-mcp_${Version}_${Platform}.zip"
$BinName = "architectos-mcp.exe"

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null
if (Test-Path $WorkDir) { Remove-Item -Recurse -Force $WorkDir }
if (Test-Path $StageDir) { Remove-Item -Recurse -Force $StageDir }
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null
New-Item -ItemType Directory -Force -Path $StageDir | Out-Null

$SpecPath = Join-Path $Root "architectos-mcp.spec"
Write-Host "Building architectos-mcp $Version ($Platform)..."
if (Get-Command pyinstaller -ErrorAction SilentlyContinue) {
    & pyinstaller --noconfirm --clean --distpath $OutDir --workpath $WorkDir $SpecPath
} else {
    & python -m PyInstaller --noconfirm --clean --distpath $OutDir --workpath $WorkDir $SpecPath
}
if ($LASTEXITCODE -ne 0) {
    Write-Error "PyInstaller failed with exit code $LASTEXITCODE"
    exit $LASTEXITCODE
}

$BinPath = Join-Path $OutDir $BinName
if (-not (Test-Path $BinPath)) {
    Write-Error "Build failed: expected binary at $BinPath"
    exit 1
}

$ReadmePath = Join-Path $OutDir "README-mcp.txt"
@"
ArchitectOS MCP Memory Server ($Version)
==========================================

Standalone stdio MCP server that opens data/architectos.db directly
(no desktop app required). See docs/MCP_MEMORY_SERVER.md for full details.

Binary
------
  $BinName   (place somewhere on PATH, or use an absolute path below)

Shared memory with the full ArchitectOS app
-------------------------------------------
Set ARCHITECTOS_ROOT to the same root the desktop app uses (directory that
contains data/architectos.db). Defaults for this binary: %USERPROFILE%\ArchitectOS

  set ARCHITECTOS_ROOT=%USERPROFILE%\ArchitectOS

Quick check
-----------
  echo {"jsonrpc":"2.0","id":1,"method":"tools/list"} | .\$BinName

Register in Cursor (%USERPROFILE%\.cursor\mcp.json or .cursor\mcp.json)
----------------------------------------------------------------------
{
  "mcpServers": {
    "architectos-memory": {
      "command": "C:\\ABSOLUTE\\PATH\\TO\\$BinName",
      "env": {
        "ARCHITECTOS_ROOT": "C:\\ABSOLUTE\\PATH\\TO\\ArchitectOS"
      }
    }
  }
}

Register in Claude Desktop
--------------------------
macOS: ~/Library/Application Support/Claude/claude_desktop_config.json
Windows: %APPDATA%\Claude\claude_desktop_config.json

{
  "mcpServers": {
    "architectos-memory": {
      "command": "C:\\ABSOLUTE\\PATH\\TO\\$BinName",
      "env": {
        "ARCHITECTOS_ROOT": "C:\\ABSOLUTE\\PATH\\TO\\ArchitectOS"
      }
    }
  }
}

Register in Claude Code (.mcp.json or ~/.claude.json)
-----------------------------------------------------
{
  "mcpServers": {
    "architectos-memory": {
      "command": "C:\\ABSOLUTE\\PATH\\TO\\$BinName",
      "env": {
        "ARCHITECTOS_ROOT": "C:\\ABSOLUTE\\PATH\\TO\\ArchitectOS"
      }
    }
  }
}

Reload the client after editing config. Tools: memory_search, memory_context,
memory_add, memory_get, memory_feedback, memory_list_projects, and related
code/memory helpers.
"@ | Set-Content -Encoding utf8 $ReadmePath

Copy-Item $BinPath (Join-Path $StageDir $BinName)
Copy-Item $ReadmePath (Join-Path $StageDir "README-mcp.txt")

$ZipPath = Join-Path $OutDir $ZipName
if (Test-Path $ZipPath) { Remove-Item -Force $ZipPath }
Compress-Archive -Path (Join-Path $StageDir "*") -DestinationPath $ZipPath
Remove-Item -Recurse -Force $StageDir

Write-Host "Wrote $BinPath"
Write-Host "Wrote $ReadmePath"
Write-Host "Wrote $ZipPath"
