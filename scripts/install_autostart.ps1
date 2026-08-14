# Install ArchitectOS HTTP server and register Windows logon autostart.
# Usage (from an unpacked share package or repo):
#   .\scripts\install_autostart.ps1
#   .\install.ps1
# Env:
#   ARCHITECTOS_ROOT, ARCHITECTOS_PORT, ARCHITECTOS_SERVER_BIN
$ErrorActionPreference = "Stop"

$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$PackageDir = $ScriptDir
if ((Split-Path -Leaf $ScriptDir) -eq "scripts") {
  $PackageDir = Resolve-Path (Join-Path $ScriptDir "..")
}

function Die([string]$Message) {
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
  "8765"
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
Write-Host "Installed server → $DestBin"
Write-Host "Data root        → $Root"

$stdoutLog = Join-Path $LogDir "server.stdout.log"
$stderrLog = Join-Path $LogDir "server.stderr.log"
@"
@echo off
set ARCHITECTOS_ROOT=$Root
cd /d "$Root"
"$DestBin" --no-browser --host 127.0.0.1 --port $Port >> "$stdoutLog" 2>> "$stderrLog"
"@ | Set-Content -LiteralPath $Wrapper -Encoding ASCII

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

# Start now
try {
  Start-ScheduledTask -TaskName $TaskName
  Write-Host "Started Scheduled Task '$TaskName'"
} catch {
  Start-Process -FilePath $Wrapper -WindowStyle Hidden
  Write-Host "Started server via wrapper"
}

Write-Host ""
Write-Host "Server will start at Windows logon. UI: http://127.0.0.1:$Port/"
Write-Host "Runtime state: $(Join-Path $DataDir 'architectos.runtime.json')"
$uninstall = Join-Path $PackageDir "uninstall.ps1"
if (-not (Test-Path $uninstall)) {
  $uninstall = Join-Path $ScriptDir "uninstall_autostart.ps1"
}
Write-Host "Uninstall: $uninstall"
Write-Host "Guide (RU): docs\GUIDE_RU.md"
