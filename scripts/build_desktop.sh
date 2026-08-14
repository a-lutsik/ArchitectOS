#!/usr/bin/env bash
# Build the ArchitectOS full desktop installer (PyInstaller sidecar + Tauri 2).
# Output artifacts under dist/desktop/
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

DESKTOP_DIR="${ROOT}/desktop"
SRC_TAURI="${DESKTOP_DIR}/src-tauri"
BINARIES_DIR="${SRC_TAURI}/binaries"
DIST_DIR="${ROOT}/dist/desktop"
WORK_DIR="${ROOT}/build/desktop-sidecar"

die() {
  echo "error: $*" >&2
  exit 1
}

need_cmd() {
  local cmd="$1"
  local hint="$2"
  command -v "$cmd" >/dev/null 2>&1 || die "${hint}"
}

have_pyinstaller() {
  command -v pyinstaller >/dev/null 2>&1 && return 0
  python3 -c "import PyInstaller" >/dev/null 2>&1 && return 0
  return 1
}

resolve_target_triple() {
  if command -v rustc >/dev/null 2>&1; then
    rustc -vV 2>/dev/null | awk '/^host:/{print $2; exit}'
    return 0
  fi
  local uname_s uname_m
  uname_s="$(uname -s)"
  uname_m="$(uname -m)"
  case "${uname_s}" in
    Darwin)
      case "${uname_m}" in
        arm64|aarch64) echo "aarch64-apple-darwin" ;;
        x86_64) echo "x86_64-apple-darwin" ;;
        *) die "Unsupported macOS arch: ${uname_m}" ;;
      esac
      ;;
    Linux)
      case "${uname_m}" in
        x86_64) echo "x86_64-unknown-linux-gnu" ;;
        aarch64|arm64) echo "aarch64-unknown-linux-gnu" ;;
        *) die "Unsupported Linux arch: ${uname_m}" ;;
      esac
      ;;
    MINGW*|MSYS*|CYGWIN*)
      echo "x86_64-pc-windows-msvc"
      ;;
    *)
      die "Cannot infer Rust target triple (install rustc or set ARCHITECTOS_TARGET_TRIPLE)."
      ;;
  esac
}

echo "==> Checking desktop build prerequisites"

need_cmd python3 "Python 3.12+ is required. Install Python and ensure python3 is on PATH."
need_cmd node "Node.js is required for the Tauri CLI. Install Node 20+ from https://nodejs.org/"
need_cmd npm "npm is required (ships with Node.js)."

if ! command -v cargo >/dev/null 2>&1 || ! command -v rustc >/dev/null 2>&1; then
  die "Rust toolchain is required (cargo + rustc). Install from https://rustup.rs/ then re-run."
fi

if ! have_pyinstaller; then
  die "PyInstaller is required for the architectos-server sidecar. Install with: pip install 'pyinstaller>=6.0'"
fi

VERSION="$(python3 -c "import tomllib; print(tomllib.load(open('pyproject.toml','rb'))['project']['version'])")"
TARGET_TRIPLE="${ARCHITECTOS_TARGET_TRIPLE:-$(resolve_target_triple)}"
case "$(uname -s)" in
  Darwin) PLATFORM="macos" ;;
  Linux) PLATFORM="linux" ;;
  MINGW*|MSYS*|CYGWIN*) PLATFORM="windows" ;;
  *) PLATFORM="$(uname -s | tr '[:upper:]' '[:lower:]')" ;;
esac

echo "==> Building architectos-server sidecar (${VERSION}, ${TARGET_TRIPLE})"
mkdir -p "${BINARIES_DIR}" "${DIST_DIR}" "${WORK_DIR}"
rm -rf "${WORK_DIR}"
mkdir -p "${WORK_DIR}"

if command -v pyinstaller >/dev/null 2>&1; then
  PYI=(pyinstaller)
else
  PYI=(python3 -m PyInstaller)
fi

"${PYI[@]}" \
  --noconfirm \
  --clean \
  --distpath "${WORK_DIR}/dist" \
  --workpath "${WORK_DIR}/work" \
  "${ROOT}/architectos-server.spec"

SIDECAR_SRC="${WORK_DIR}/dist/architectos-server"
[[ -f "${SIDECAR_SRC}" ]] || die "PyInstaller did not produce ${SIDECAR_SRC}"

SIDECAR_DEST="${BINARIES_DIR}/architectos-server-${TARGET_TRIPLE}"
cp -f "${SIDECAR_SRC}" "${SIDECAR_DEST}"
chmod +x "${SIDECAR_DEST}"
# Convenience copy without triple for local runs.
cp -f "${SIDECAR_SRC}" "${BINARIES_DIR}/architectos-server"
chmod +x "${BINARIES_DIR}/architectos-server"
echo "    Sidecar → ${SIDECAR_DEST}"

echo "==> Installing Tauri CLI deps (desktop/)"
(
  cd "${DESKTOP_DIR}"
  if [[ ! -d node_modules/@tauri-apps/cli ]]; then
    npm install
  fi
)

echo "==> cargo tauri build"
(
  cd "${DESKTOP_DIR}"
  npm run build
)

BUNDLE_DIR="${SRC_TAURI}/target/release/bundle"
mkdir -p "${DIST_DIR}"

copied=0
if [[ -d "${BUNDLE_DIR}" ]]; then
  while IFS= read -r -d '' artifact; do
    base="$(basename "${artifact}")"
    dest="${DIST_DIR}/${base}"
    # Prefer versioned names when copying dmg/msi/exe
    case "${base}" in
      *.dmg)
        dest="${DIST_DIR}/ArchitectOS_${VERSION}_macos.dmg"
        ;;
      *.msi)
        dest="${DIST_DIR}/ArchitectOS_${VERSION}_windows.msi"
        ;;
      *.exe)
        dest="${DIST_DIR}/ArchitectOS_${VERSION}_windows.exe"
        ;;
    esac
    cp -f "${artifact}" "${dest}"
    echo "    Copied ${dest}"
    copied=$((copied + 1))
  done < <(find "${BUNDLE_DIR}" -type f \( -name '*.dmg' -o -name '*.msi' -o -name '*.exe' -o -name '*.app.tar.gz' \) -print0 2>/dev/null || true)
fi

# Always keep a pointer to the raw bundle tree for debugging.
echo "${BUNDLE_DIR}" > "${DIST_DIR}/bundle_path.txt"
echo "Wrote sidecar + Tauri bundle metadata under ${DIST_DIR}/"
if [[ "${copied}" -eq 0 ]]; then
  echo "warning: no dmg/msi/exe found under ${BUNDLE_DIR}; check tauri build logs." >&2
  echo "         Unsigned local binaries may still be under ${SRC_TAURI}/target/release/" >&2
fi

echo "==> Desktop build complete (${PLATFORM})"
