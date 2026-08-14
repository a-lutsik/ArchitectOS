#!/usr/bin/env bash
# Build the ArchitectOS IntelliJ plugin and copy the zip to dist/ide/.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IDE_DIR="${ROOT}/ide/intellij"
DIST_DIR="${ROOT}/dist/ide"
GRADLEW="${IDE_DIR}/gradlew"
BUILD_GRADLE="${IDE_DIR}/build.gradle.kts"
GRADLE_PROPS="${IDE_DIR}/gradle.properties"

die() {
  echo "error: $*" >&2
  exit 1
}

require_file() {
  local path="$1"
  local hint="$2"
  [[ -f "$path" ]] || die "${hint}"
}

java_works() {
  local java_bin="$1"
  [[ -x "$java_bin" ]] || return 1
  "$java_bin" -version >/dev/null 2>&1
}

resolve_java_home() {
  local candidate=""

  if [[ -n "${JAVA_HOME:-}" ]]; then
    candidate="${JAVA_HOME}"
    if java_works "${candidate}/bin/java"; then
      echo "${candidate}"
      return 0
    fi
  fi

  if command -v java >/dev/null 2>&1; then
    if java -version >/dev/null 2>&1; then
      if command -v /usr/libexec/java_home >/dev/null 2>&1; then
        candidate="$(/usr/libexec/java_home 2>/dev/null || true)"
        if [[ -n "${candidate}" ]] && java_works "${candidate}/bin/java"; then
          echo "${candidate}"
          return 0
        fi
      fi
      # java is on PATH but JAVA_HOME unknown — let Gradle use PATH
      echo ""
      return 0
    fi
  fi

  if [[ -f "${GRADLE_PROPS}" ]]; then
    candidate="$(
      sed -n 's/^[[:space:]]*org\.gradle\.java\.home[[:space:]]*=[[:space:]]*//p' "${GRADLE_PROPS}" \
        | head -n 1 \
        | tr -d '\r' \
        | sed 's/[[:space:]]*$//'
    )"
    if [[ -n "${candidate}" ]] && java_works "${candidate}/bin/java"; then
      echo "${candidate}"
      return 0
    fi
  fi

  return 1
}

require_file "${GRADLEW}" \
  "Gradle wrapper missing at ide/intellij/gradlew. Clone the full repo or restore ide/intellij/gradlew + gradle/wrapper/."
require_file "${IDE_DIR}/gradle/wrapper/gradle-wrapper.jar" \
  "Gradle wrapper jar missing at ide/intellij/gradle/wrapper/gradle-wrapper.jar."
require_file "${BUILD_GRADLE}" \
  "Missing ide/intellij/build.gradle.kts — IntelliJ plugin project not found."

[[ -x "${GRADLEW}" ]] || chmod +x "${GRADLEW}"

if ! JAVA_HOME_RESOLVED="$(resolve_java_home)"; then
  die "JDK 17+ not found. Install a JDK and set JAVA_HOME, or add org.gradle.java.home in ide/intellij/gradle.properties."
fi

if [[ -n "${JAVA_HOME_RESOLVED}" ]]; then
  export JAVA_HOME="${JAVA_HOME_RESOLVED}"
  export PATH="${JAVA_HOME}/bin:${PATH}"
fi

VERSION="$(
  sed -n 's/^[[:space:]]*version[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1/p' "${BUILD_GRADLE}" \
    | head -n 1
)"
[[ -n "${VERSION}" ]] || die "Could not read version from ide/intellij/build.gradle.kts"

echo "==> Building IntelliJ plugin (version ${VERSION})"
(
  cd "${IDE_DIR}"
  ./gradlew --no-daemon buildPlugin
)

DIST_ZIP_SRC=""
for candidate in \
  "${IDE_DIR}/build/distributions/architectos-memory-${VERSION}.zip" \
  "${IDE_DIR}/build/distributions/"*"-${VERSION}.zip"
do
  if [[ -f "${candidate}" ]]; then
    DIST_ZIP_SRC="${candidate}"
    break
  fi
done

if [[ -z "${DIST_ZIP_SRC}" ]]; then
  shopt -s nullglob
  zips=("${IDE_DIR}/build/distributions/"*.zip)
  shopt -u nullglob
  if [[ ${#zips[@]} -eq 1 ]]; then
    DIST_ZIP_SRC="${zips[0]}"
  elif [[ ${#zips[@]} -gt 1 ]]; then
    die "Multiple zips in ide/intellij/build/distributions/; expected architectos-memory-${VERSION}.zip"
  else
    die "Gradle buildPlugin succeeded but no zip found under ide/intellij/build/distributions/"
  fi
fi

mkdir -p "${DIST_DIR}"
OUT_ZIP="${DIST_DIR}/ArchitectOS-Memory-${VERSION}.zip"
cp -f "${DIST_ZIP_SRC}" "${OUT_ZIP}"

echo "==> IDE plugin ready: ${OUT_ZIP}"
echo "    Install via Settings → Plugins → ⚙ → Install Plugin from Disk"
echo "    (requires a running ArchitectOS HTTP server; use Detect for URL/token)"
