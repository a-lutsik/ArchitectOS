# Build the ArchitectOS full desktop installer (PyInstaller sidecar + Tauri 2).
# Output artifacts under dist/desktop/
$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
Set-Location $Root

$DesktopDir = Join-Path $Root "desktop"
$SrcTauri = Join-Path $DesktopDir "src-tauri"
$BinariesDir = Join-Path $SrcTauri "binaries"
$DistDir = Join-Path $Root "dist\desktop"
$WorkDir = Join-Path $Root "build\desktop-sidecar"

function Die([string]$Message) {
  Write-Error $Message
  exit 1
}

function Need-Command([string]$Name, [string]$Hint) {
  if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
    Die $Hint
  }
}

function Have-PyInstaller {
  if (Get-Command pyinstaller -ErrorAction SilentlyContinue) { return $true }
  try {
    & python -c "import PyInstaller" 2>$null
    return ($LASTEXITCODE -eq 0)
  } catch {
    return $false
  }
}

Write-Host "==> Checking desktop build prerequisites"
Need-Command python "Python 3.12+ is required. Install Python and ensure python is on PATH."
Need-Command node "Node.js is required for the Tauri CLI. Install Node 20+ from https://nodejs.org/"
Need-Command npm "npm is required (ships with Node.js)."

if (-not (Get-Command cargo -ErrorAction SilentlyContinue) -or -not (Get-Command rustc -ErrorAction SilentlyContinue)) {
  Die "Rust toolchain is required (cargo + rustc). Install from https://rustup.rs/ then re-run."
}

if (-not (Have-PyInstaller)) {
  Die "PyInstaller is required for the architectos-server sidecar. Install with: pip install 'pyinstaller>=6.0'"
}

$Version = & python -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])"
$TargetTriple = if ($env:ARCHITECTOS_TARGET_TRIPLE) { $env:ARCHITECTOS_TARGET_TRIPLE } else { "x86_64-pc-windows-msvc" }
if (-not $env:ARCHITECTOS_TARGET_TRIPLE -and (Get-Command rustc -ErrorAction SilentlyContinue)) {
  $hostLine = & rustc -vV | Select-String '^host:'
  if ($hostLine) {
    $TargetTriple = ($hostLine.ToString() -replace '^host:\s+', '').Trim()
  }
}

Write-Host "==> Building architectos-server sidecar ($Version, $TargetTriple)"
New-Item -ItemType Directory -Force -Path $BinariesDir, $DistDir, $WorkDir | Out-Null
if (Test-Path $WorkDir) { Remove-Item -Recurse -Force $WorkDir }
New-Item -ItemType Directory -Force -Path $WorkDir | Out-Null

$PyIArgs = @(
  "--noconfirm",
  "--clean",
  "--distpath", (Join-Path $WorkDir "dist"),
  "--workpath", (Join-Path $WorkDir "work"),
  (Join-Path $Root "architectos-server.spec")
)
if (Get-Command pyinstaller -ErrorAction SilentlyContinue) {
  & pyinstaller @PyIArgs
} else {
  & python -m PyInstaller @PyIArgs
}
if ($LASTEXITCODE -ne 0) { Die "PyInstaller failed" }

$SidecarSrc = Join-Path $WorkDir "dist\architectos-server.exe"
if (-not (Test-Path $SidecarSrc)) {
  $SidecarSrc = Join-Path $WorkDir "dist\architectos-server"
}
if (-not (Test-Path $SidecarSrc)) {
  Die "PyInstaller did not produce architectos-server.exe"
}

$SidecarDest = Join-Path $BinariesDir "architectos-server-$TargetTriple.exe"
Copy-Item -Force $SidecarSrc $SidecarDest
Copy-Item -Force $SidecarSrc (Join-Path $BinariesDir "architectos-server.exe")
Write-Host "    Sidecar → $SidecarDest"

Write-Host "==> Installing Tauri CLI deps (desktop/)"
Push-Location $DesktopDir
try {
  if (-not (Test-Path "node_modules\@tauri-apps\cli")) {
    npm install
    if ($LASTEXITCODE -ne 0) { Die "npm install failed in desktop/" }
  }
  Write-Host "==> cargo tauri build"
  npm run build
  if ($LASTEXITCODE -ne 0) { Die "tauri build failed" }
} finally {
  Pop-Location
}

$BundleDir = Join-Path $SrcTauri "target\release\bundle"
New-Item -ItemType Directory -Force -Path $DistDir | Out-Null
$copied = 0
if (Test-Path $BundleDir) {
  Get-ChildItem -Path $BundleDir -Recurse -Include *.msi,*.exe | ForEach-Object {
    $dest = Join-Path $DistDir $_.Name
    if ($_.Extension -eq ".msi") {
      $dest = Join-Path $DistDir "ArchitectOS_${Version}_windows.msi"
    } elseif ($_.Name -match 'nsis|setup' -or $_.Extension -eq ".exe") {
      $dest = Join-Path $DistDir "ArchitectOS_${Version}_windows.exe"
    }
    Copy-Item -Force $_.FullName $dest
    Write-Host "    Copied $dest"
    $copied++
  }
}

Set-Content -Path (Join-Path $DistDir "bundle_path.txt") -Value $BundleDir
Write-Host "Wrote sidecar + Tauri bundle metadata under $DistDir\"
if ($copied -eq 0) {
  Write-Warning "No msi/exe found under $BundleDir; check tauri build logs."
  Write-Warning "Unsigned local binaries may still be under $SrcTauri\target\release\"
}

Write-Host "==> Desktop build complete (windows)"
