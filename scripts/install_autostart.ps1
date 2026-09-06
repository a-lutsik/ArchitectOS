# Install ArchitectOS HTTP server and register Windows logon autostart.
# Usage (from an unpacked share package or repo):
#   .\scripts\install_autostart.ps1
#   .\install.ps1
# Env:
#   ARCHITECTOS_ROOT, ARCHITECTOS_PORT, ARCHITECTOS_SERVER_BIN
#   ARCHITECTOS_SKIP_AUTOSTART=1 — copy files only (no Scheduled Task / HKCU Run)
#   ARCHITECTOS_PROBE_TIMEOUT — seconds for sqlite-vec probe (default 45)
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PackageDir = $ScriptDir
if ((Split-Path -Leaf $ScriptDir) -eq "scripts") {
  $PackageDir = Resolve-Path (Join-Path $ScriptDir "..")
}

function Die([string]$Message) {
  Write-Host ""
  Write-Host "========================================"
  Write-Host "  ERROR / ОШИБКА"
  Write-Host "========================================"
  Write-Host "  $Message"
  Write-Error $Message
  exit 1
}

$Root = if ($env:ARCHITECTOS_ROOT -and $env:ARCHITECTOS_ROOT.Trim()) {
  $env:ARCHITECTOS_ROOT.Trim()
} elseif ($env:LOCALAPPDATA) {
  Join-Path $env:LOCALAPPDATA "ArchitectOS"
} else {
  Join-Path $env:USERPROFILE "ArchitectOS"
}

$Port = if ($env:ARCHITECTOS_PORT -and $env:ARCHITECTOS_PORT.Trim()) {
  $env:ARCHITECTOS_PORT.Trim()
} else {
  "8766"
}

$BinDir = Join-Path $Root "bin"
$DataDir = Join-Path $Root "data"
$LogDir = Join-Path $Root "logs"
$DestBin = Join-Path $BinDir "architectos-server.exe"
$TaskName = "ArchitectOS Server"
$Wrapper = Join-Path $BinDir "start-architectos-server.cmd"

function Resolve-ServerBin {
  if ($env:ARCHITECTOS_SERVER_BIN -and (Test-Path -LiteralPath $env:ARCHITECTOS_SERVER_BIN)) {
    return (Resolve-Path -LiteralPath $env:ARCHITECTOS_SERVER_BIN).Path
  }
  $candidates = @(
    (Join-Path $PackageDir "architectos-server.exe"),
    (Join-Path $PackageDir "architectos-server"),
    (Join-Path $PackageDir "bin\architectos-server.exe"),
    $DestBin
  )
  foreach ($c in $candidates) {
    if (Test-Path -LiteralPath $c) {
      return (Resolve-Path -LiteralPath $c).Path
    }
  }
  Die "architectos-server.exe not found next to this script. Build with scripts/build_share_package.py or set ARCHITECTOS_SERVER_BIN."
}

$SrcBin = Resolve-ServerBin
New-Item -ItemType Directory -Force -Path $BinDir, $DataDir, $LogDir | Out-Null
Copy-Item -LiteralPath $SrcBin -Destination $DestBin -Force
Unblock-File -LiteralPath $DestBin -ErrorAction SilentlyContinue
$srcRuntime = Join-Path (Split-Path -Parent $SrcBin) "runtime"
if (Test-Path -LiteralPath $srcRuntime) {
  $dstRuntime = Join-Path $BinDir "runtime"
  if (Test-Path -LiteralPath $dstRuntime) {
    Remove-Item -LiteralPath $dstRuntime -Recurse -Force
  }
  Copy-Item -LiteralPath $srcRuntime -Destination $dstRuntime -Recurse -Force
  Get-ChildItem -LiteralPath $dstRuntime -Recurse -File -Filter "*.exe" -ErrorAction SilentlyContinue |
    ForEach-Object { Unblock-File -LiteralPath $_.FullName -ErrorAction SilentlyContinue }
  Write-Host "Installed runtime → $dstRuntime"
}
Write-Host "Installed server → $DestBin"
Write-Host "Data root        → $Root"

function Copy-IfPresent([string]$Src, [string]$Dst) {
  if (Test-Path -LiteralPath $Src) {
    $destDir = Split-Path -Parent $Dst
    if ($destDir) {
      New-Item -ItemType Directory -Force -Path $destDir | Out-Null
    }
    Copy-Item -LiteralPath $Src -Destination $Dst -Force
  }
}

$uninstallSrc = Join-Path $PackageDir "uninstall.ps1"
if (-not (Test-Path -LiteralPath $uninstallSrc)) {
  $uninstallSrc = Join-Path $ScriptDir "uninstall_autostart.ps1"
}
if (Test-Path -LiteralPath $uninstallSrc) {
  Copy-Item -LiteralPath $uninstallSrc -Destination (Join-Path $Root "uninstall.ps1") -Force
}
$docsSrc = Join-Path $PackageDir "docs"
if (Test-Path -LiteralPath $docsSrc) {
  New-Item -ItemType Directory -Force -Path (Join-Path $Root "docs") | Out-Null
  Copy-Item -Path (Join-Path $docsSrc "*") -Destination (Join-Path $Root "docs") -Force -ErrorAction SilentlyContinue
}
foreach ($name in @("README-SHARE.txt", "README-mcp.txt")) {
  Copy-IfPresent (Join-Path $PackageDir $name) (Join-Path $Root $name)
}
foreach ($mcp in @("architectos-mcp.exe", "architectos-mcp")) {
  $mcpSrc = Join-Path $PackageDir $mcp
  if (Test-Path -LiteralPath $mcpSrc) {
    $mcpDst = Join-Path $BinDir $mcp
    Copy-Item -LiteralPath $mcpSrc -Destination $mcpDst -Force
    Unblock-File -LiteralPath $mcpDst -ErrorAction SilentlyContinue
    Write-Host "Installed MCP    → $mcpDst"
  }
}
$ideSrc = Join-Path $PackageDir "ide"
if (Test-Path -LiteralPath $ideSrc) {
  $ideDst = Join-Path $Root "ide"
  New-Item -ItemType Directory -Force -Path $ideDst | Out-Null
  Copy-Item -Path (Join-Path $ideSrc "*") -Destination $ideDst -Force -Recurse -ErrorAction SilentlyContinue
  Write-Host "Installed IDE zip → $ideDst"
}

$probeTimeoutSec = 45
if ($env:ARCHITECTOS_PROBE_TIMEOUT -and $env:ARCHITECTOS_PROBE_TIMEOUT.Trim()) {
  $probeTimeoutSec = [int]$env:ARCHITECTOS_PROBE_TIMEOUT.Trim()
}

function Invoke-VectorRuntimeProbe([string]$Bin, [int]$TimeoutSec) {
  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = $Bin
  $psi.Arguments = "vector-runtime"
  $psi.UseShellExecute = $false
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  $proc = [System.Diagnostics.Process]::Start($psi)
  if (-not $proc.WaitForExit($TimeoutSec * 1000)) {
    try { $proc.Kill() } catch {}
    Write-Host "sqlite-vec probe timed out; continuing install."
    return $false
  }
  $stdout = $proc.StandardOutput.ReadToEnd()
  $stderr = $proc.StandardError.ReadToEnd()
  if ($stdout) { Write-Host $stdout.TrimEnd() }
  if ($stderr) { Write-Host $stderr.TrimEnd() }
  return ($proc.ExitCode -eq 0)
}

Write-Host ""
Write-Host "==> Memory search (sqlite-vec)"
try {
  if (-not (Invoke-VectorRuntimeProbe $DestBin $probeTimeoutSec)) {
    Write-Host "sqlite-vec probe unavailable in this binary; Ask/Search still work (Python fallback)."
  }
} catch {
  Write-Host "sqlite-vec probe unavailable in this binary; Ask/Search still work (Python fallback)."
}

$stdoutLog = Join-Path $LogDir "server.stdout.log"
$stderrLog = Join-Path $LogDir "server.stderr.log"
@"
@echo off
set ARCHITECTOS_ROOT=$Root
cd /d "$Root"
"$DestBin" --no-browser --host 127.0.0.1 --port $Port >> "$stdoutLog" 2>> "$stderrLog"
"@ | Set-Content -LiteralPath $Wrapper -Encoding ASCII

$skipAutostart = $env:ARCHITECTOS_SKIP_AUTOSTART -and $env:ARCHITECTOS_SKIP_AUTOSTART.Trim() -eq "1"
$noStartNow = $env:ARCHITECTOS_NO_START_NOW -and $env:ARCHITECTOS_NO_START_NOW.Trim() -eq "1"
$askAutostart = -not $skipAutostart
$askStartNow = -not $noStartNow

# Interactive prompts when running in a console
try {
  if ([Environment]::UserInteractive -and -not $env:ARCHITECTOS_SKIP_PAUSE) {
    Write-Host ""
    Write-Host "Setup choices / Параметры установки"
    $ansNow = Read-Host "Start ArchitectOS server now? [Y/n] / Запустить сервер сейчас? [Y/n]"
    if ($ansNow -match '^(n|no)$') { $askStartNow = $false } else { $askStartNow = $true }
    $ansAuto = Read-Host "Start server automatically at login? [Y/n] / Автозапуск при входе в систему? [Y/n]"
    if ($ansAuto -match '^(n|no)$') { $askAutostart = $false } else { $askAutostart = $true }
  }
} catch { }

if (-not $askAutostart) {
  Write-Host ""
  Write-Host "Skipped OS autostart."
  Write-Host "Start later: $Wrapper"
  if ($askStartNow) {
    Start-Process -FilePath $Wrapper -WindowStyle Hidden
    Write-Host "Started server now via wrapper"
  }
} else {
  # Prefer Scheduled Task at logon (survives better than Run key alone)
  $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  if ($existing) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
  }

  $action = New-ScheduledTaskAction -Execute $Wrapper
  $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
  $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
  $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
  Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null

  # Also register HKCU Run as a lightweight fallback if Task Scheduler is blocked
  $runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
  New-ItemProperty -Path $runKey -Name "ArchitectOSServer" -Value "`"$Wrapper`"" -PropertyType String -Force | Out-Null

  Write-Host "Autostart registered: Scheduled Task '$TaskName' + HKCU Run\ArchitectOSServer"

  if ($askStartNow) {
    try {
      Start-ScheduledTask -TaskName $TaskName
      Write-Host "Started Scheduled Task '$TaskName'"
    } catch {
      Start-Process -FilePath $Wrapper -WindowStyle Hidden
      Write-Host "Started server via wrapper"
    }
  } else {
    Write-Host "Server will start at the next Windows logon (not started now)."
  }
}

Write-Host ""
Write-Host "========================================"
Write-Host "  SUCCESS / УСПЕХ"
Write-Host "========================================"
if ($askAutostart) {
  Write-Host "Server will start at Windows logon."
} else {
  Write-Host "Files installed under $Root (no login autostart)."
}
Write-Host "UI: http://127.0.0.1:$Port/"
Write-Host "Runtime state: $(Join-Path $DataDir 'architectos.runtime.json')"
$uninstall = Join-Path $Root "uninstall.ps1"
if (-not (Test-Path $uninstall)) {
  $uninstall = Join-Path $PackageDir "uninstall.ps1"
  if (-not (Test-Path $uninstall)) {
    $uninstall = Join-Path $ScriptDir "uninstall_autostart.ps1"
  }
}
Write-Host "Uninstall: $uninstall"
$guide = Join-Path $Root "docs\GUIDE_RU.md"
if (Test-Path -LiteralPath $guide) {
  Write-Host "Guide (RU): $guide"
} else {
  Write-Host "Guide (RU): docs\GUIDE_RU.md"
}
