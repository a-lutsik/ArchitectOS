# Remove ArchitectOS server Windows logon autostart.
$ErrorActionPreference = "Continue"

$Root = if ($env:ARCHITECTOS_ROOT -and $env:ARCHITECTOS_ROOT.Trim()) {
  $env:ARCHITECTOS_ROOT.Trim()
} elseif ($env:LOCALAPPDATA) {
  Join-Path $env:LOCALAPPDATA "ArchitectOS"
} else {
  Join-Path $env:USERPROFILE "ArchitectOS"
}

$TaskName = "ArchitectOS Server"
$BinDir = Join-Path $Root "bin"
$DestBin = Join-Path $BinDir "architectos-server.exe"
$Wrapper = Join-Path $BinDir "start-architectos-server.cmd"

$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
  Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
  Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
  Write-Host "Removed Scheduled Task '$TaskName'"
}

$runKey = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run"
if (Get-ItemProperty -Path $runKey -Name "ArchitectOSServer" -ErrorAction SilentlyContinue) {
  Remove-ItemProperty -Path $runKey -Name "ArchitectOSServer" -Force -ErrorAction SilentlyContinue
  Write-Host "Removed HKCU Run\ArchitectOSServer"
}

$runtime = Join-Path $Root "data\architectos.runtime.json"
if (Test-Path -LiteralPath $runtime) {
  try {
    $state = Get-Content -LiteralPath $runtime -Raw | ConvertFrom-Json
    if ($state.pid) {
      Stop-Process -Id ([int]$state.pid) -Force -ErrorAction SilentlyContinue
      Write-Host "Stopped server pid $($state.pid)"
    }
  } catch {}
}

# Best-effort: stop processes matching the installed binary
Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
  Where-Object { $_.ExecutablePath -and ($_.ExecutablePath -ieq $DestBin) } |
  ForEach-Object {
    Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    Write-Host "Stopped process $($_.ProcessId)"
  }

if ($env:ARCHITECTOS_REMOVE_BIN -eq "1") {
  Remove-Item -LiteralPath $DestBin, $Wrapper -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath (Join-Path $BinDir "architectos-mcp.exe") -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath (Join-Path $BinDir "runtime") -Recurse -Force -ErrorAction SilentlyContinue
  Write-Host "Removed installed binaries under $BinDir"
}

Write-Host ""
Write-Host "========================================"
Write-Host "  SUCCESS / УСПЕХ"
Write-Host "========================================"
Write-Host "Autostart disabled. Data kept under $Root (delete manually if desired)."
