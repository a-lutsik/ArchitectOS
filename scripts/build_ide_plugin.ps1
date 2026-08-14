# Build the ArchitectOS IntelliJ plugin and copy the zip to dist/ide/.
# Requires JDK 17+ and the Gradle wrapper under ide/intellij.
$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$IdeDir = Join-Path $Root "ide\intellij"
$DistDir = Join-Path $Root "dist\ide"
$GradlewBat = Join-Path $IdeDir "gradlew.bat"
$BuildGradle = Join-Path $IdeDir "build.gradle.kts"
$GradleProps = Join-Path $IdeDir "gradle.properties"
$WrapperJar = Join-Path $IdeDir "gradle\wrapper\gradle-wrapper.jar"

function Die([string]$Message) {
    Write-Error "error: $Message"
    exit 1
}

function Test-JavaHome([string]$HomePath) {
    if ([string]::IsNullOrWhiteSpace($HomePath)) { return $false }
    $javaExe = Join-Path $HomePath "bin\java.exe"
    if (-not (Test-Path $javaExe)) { return $false }
    try {
        & $javaExe -version 2>&1 | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Resolve-JavaHome {
    if ($env:JAVA_HOME -and (Test-JavaHome $env:JAVA_HOME)) {
        return $env:JAVA_HOME
    }

    $javaCmd = Get-Command java -ErrorAction SilentlyContinue
    if ($javaCmd) {
        try {
            & java -version 2>&1 | Out-Null
            if ($LASTEXITCODE -eq 0) {
                return ""  # usable on PATH; leave JAVA_HOME unset for Gradle
            }
        } catch { }
    }

    if (Test-Path $GradleProps) {
        $line = Get-Content $GradleProps |
            Where-Object { $_ -match '^\s*org\.gradle\.java\.home\s*=' } |
            Select-Object -First 1
        if ($line) {
            $home = ($line -split '=', 2)[1].Trim()
            if (Test-JavaHome $home) { return $home }
        }
    }

    return $null
}

if (-not (Test-Path $GradlewBat)) {
    Die "Gradle wrapper missing at ide/intellij/gradlew.bat. Clone the full repo or restore ide/intellij/gradlew.bat + gradle/wrapper/."
}
if (-not (Test-Path $WrapperJar)) {
    Die "Gradle wrapper jar missing at ide/intellij/gradle/wrapper/gradle-wrapper.jar."
}
if (-not (Test-Path $BuildGradle)) {
    Die "Missing ide/intellij/build.gradle.kts — IntelliJ plugin project not found."
}

$resolvedHome = Resolve-JavaHome
if ($null -eq $resolvedHome) {
    Die "JDK 17+ not found. Install a JDK and set JAVA_HOME, or add org.gradle.java.home in ide/intellij/gradle.properties."
}
if ($resolvedHome) {
    $env:JAVA_HOME = $resolvedHome
    $env:Path = "$(Join-Path $resolvedHome 'bin');$env:Path"
}

$versionLine = Get-Content $BuildGradle |
    Where-Object { $_ -match '^\s*version\s*=\s*"([^"]+)"' } |
    Select-Object -First 1
if (-not $versionLine -or $versionLine -notmatch '^\s*version\s*=\s*"([^"]+)"') {
    Die "Could not read version from ide/intellij/build.gradle.kts"
}
$Version = $Matches[1]

Write-Host "==> Building IntelliJ plugin (version $Version)"
Push-Location $IdeDir
try {
    & .\gradlew.bat --no-daemon buildPlugin
    if ($LASTEXITCODE -ne 0) {
        Die "gradlew buildPlugin failed with exit code $LASTEXITCODE"
    }
} finally {
    Pop-Location
}

$distributions = Join-Path $IdeDir "build\distributions"
$preferred = Join-Path $distributions "architectos-memory-$Version.zip"
$srcZip = $null
if (Test-Path $preferred) {
    $srcZip = $preferred
} else {
    $matches = @(Get-ChildItem -Path $distributions -Filter "*-$Version.zip" -ErrorAction SilentlyContinue)
    if ($matches.Count -eq 1) {
        $srcZip = $matches[0].FullName
    } elseif ($matches.Count -eq 0) {
        $any = @(Get-ChildItem -Path $distributions -Filter "*.zip" -ErrorAction SilentlyContinue)
        if ($any.Count -eq 1) {
            $srcZip = $any[0].FullName
        } else {
            Die "Gradle buildPlugin succeeded but no zip found under ide/intellij/build/distributions/"
        }
    } else {
        Die "Multiple zips in ide/intellij/build/distributions/; expected architectos-memory-$Version.zip"
    }
}

New-Item -ItemType Directory -Force -Path $DistDir | Out-Null
$outZip = Join-Path $DistDir "ArchitectOS-Memory-$Version.zip"
Copy-Item -Force $srcZip $outZip

Write-Host "==> IDE plugin ready: $outZip"
Write-Host "    Install via Settings → Plugins → gear → Install Plugin from Disk"
Write-Host "    (requires a running ArchitectOS HTTP server; use Detect for URL/token)"
