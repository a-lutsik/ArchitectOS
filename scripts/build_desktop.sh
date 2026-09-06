#!/usr/bin/env bash
# Build the ArchitectOS full desktop installer (PyInstaller sidecar + Tauri 2).
# Output artifacts under dist/desktop/
#
#   ./scripts/build_desktop.sh              # host architecture
#   ./scripts/build_desktop.sh --arch intel
#   ./scripts/build_desktop.sh --arch arm64
#   ./scripts/build_desktop.sh --arch all   # macOS: Intel + Apple Silicon DMGs
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -d "${HOME}/.cargo/bin" ]]; then
  export PATH="${HOME}/.cargo/bin:${PATH}"
fi

DESKTOP_DIR="${ROOT}/desktop"
SRC_TAURI="${DESKTOP_DIR}/src-tauri"
BINARIES_DIR="${SRC_TAURI}/binaries"
DIST_DIR="${ROOT}/dist/desktop"

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

resolve_host_triple() {
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

macos_dmg_suffix() {
  case "$1" in
    aarch64-apple-darwin) echo "macos_arm64" ;;
    x86_64-apple-darwin) echo "macos_intel" ;;
    *) echo "macos" ;;
  esac
}

pyi_target_arch() {
  case "$1" in
    aarch64-apple-darwin) echo "arm64" ;;
    x86_64-apple-darwin) echo "x86_64" ;;
    *) echo "" ;;
  esac
}

usage() {
  cat <<'EOF'
Build the ArchitectOS desktop installer (PyInstaller sidecar + Tauri 2).

Usage:
  ./scripts/build_desktop.sh
  ./scripts/build_desktop.sh --arch intel|arm64|all

macOS artifacts:
  dist/desktop/ArchitectOS_<ver>_macos_intel.dmg
  dist/desktop/ArchitectOS_<ver>_macos_arm64.dmg
EOF
}

ARCH_ARG=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --arch)
      ARCH_ARG="${2:-}"
      [[ -n "${ARCH_ARG}" ]] || die "--arch requires intel, arm64, or all"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "Unknown argument: $1 (try --help)"
      ;;
  esac
done

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
HOST_TRIPLE="$(resolve_host_triple)"
case "$(uname -s)" in
  Darwin) PLATFORM="macos" ;;
  Linux) PLATFORM="linux" ;;
  MINGW*|MSYS*|CYGWIN*) PLATFORM="windows" ;;
  *) PLATFORM="$(uname -s | tr '[:upper:]' '[:lower:]')" ;;
esac

triples_from_arch_arg() {
  local arg="$1"
  case "${arg}" in
    "" )
      echo "${ARCHITECTOS_TARGET_TRIPLE:-${HOST_TRIPLE}}"
      ;;
    intel|x86_64|x64)
      [[ "${PLATFORM}" == "macos" ]] || die "--arch ${arg} is only supported on macOS"
      echo "x86_64-apple-darwin"
      ;;
    arm64|aarch64|apple|silicon)
      [[ "${PLATFORM}" == "macos" ]] || die "--arch ${arg} is only supported on macOS"
      echo "aarch64-apple-darwin"
      ;;
    all|both)
      [[ "${PLATFORM}" == "macos" ]] || die "--arch all is only supported on macOS"
      echo "x86_64-apple-darwin"
      echo "aarch64-apple-darwin"
      ;;
    *)
      die "Unknown --arch ${arg} (use intel, arm64, or all)"
      ;;
  esac
}

ensure_native_overlay() {
  local work_dir="$1"
  local overlay="${work_dir}/native-overlay"
  python3 - "${overlay}" <<'PY'
import importlib.metadata as metadata
import subprocess
import sys
import zipfile
from pathlib import Path

overlay = Path(sys.argv[1])
overlay.mkdir(parents=True, exist_ok=True)
wheel_dir = overlay.parent / "native-wheels"
wheel_dir.mkdir(parents=True, exist_ok=True)

packages = ("numpy", "charset-normalizer", "sqlite-vec")
platforms = (
    "macosx_14_0_arm64",
    "macosx_13_0_arm64",
    "macosx_12_0_arm64",
    "macosx_11_0_arm64",
    "macosx_10_13_universal2",
    "macosx_10_9_universal2",
)

def pkg_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return ""

for name in packages:
    version = pkg_version(name)
    spec = f"{name}=={version}" if version else name
    saved = False
    for plat in platforms:
        cmd = [
            sys.executable, "-m", "pip", "download", spec,
            "--platform", plat, "--python-version", "312",
            "--only-binary=:all:", "--no-deps", "-d", str(wheel_dir),
        ]
        result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if result.returncode == 0:
            saved = True
            break
    if not saved:
        raise SystemExit(f"could not download {spec} for macOS arm64/universal2")

for wheel in sorted(wheel_dir.glob("*.whl")):
    with zipfile.ZipFile(wheel) as zf:
        for name in zf.namelist():
            if name.endswith((".so", ".dylib")) and ".dist-info/" not in name:
                dest = overlay / name
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(zf.read(name))
print(overlay)
PY
}

build_one() {
  local TARGET_TRIPLE="$1"
  local WORK_DIR="${ROOT}/build/desktop-sidecar-${TARGET_TRIPLE}"
  local PYI_ARCH DMG_SUFFIX DEST_DMG SIDECAR_SRC SIDECAR_DEST
  local BUNDLE_DIR APP_BUNDLE tauri_ec tauri_dmg
  local PYI=()

  PYI_ARCH="$(pyi_target_arch "${TARGET_TRIPLE}")"
  DMG_SUFFIX="$(macos_dmg_suffix "${TARGET_TRIPLE}")"
  DEST_DMG="${DIST_DIR}/ArchitectOS_${VERSION}_${DMG_SUFFIX}.dmg"

  if command -v rustup >/dev/null 2>&1; then
    rustup target add "${TARGET_TRIPLE}" >/dev/null
  fi

  echo "==> sqlite-vec build probe"
  python3 - <<'PY'
import sys
sys.path.insert(0, "backend")
from architectos.vec_runtime import emit_builder_probe
raise SystemExit(emit_builder_probe())
PY

  echo "==> Building architectos-server sidecar (${VERSION}, ${TARGET_TRIPLE})"
  mkdir -p "${BINARIES_DIR}" "${DIST_DIR}"
  rm -rf "${WORK_DIR}"
  mkdir -p "${WORK_DIR}"

  unset ARCHITECTOS_SQLITE_VEC_DYLIB || true
  unset ARCHITECTOS_PYI_NATIVE_OVERLAY || true
  if [[ "${TARGET_TRIPLE}" == "aarch64-apple-darwin" ]]; then
    ARCHITECTOS_PYI_NATIVE_OVERLAY="$(ensure_native_overlay "${WORK_DIR}")"
    export ARCHITECTOS_PYI_NATIVE_OVERLAY
    echo "    native overlay → ${ARCHITECTOS_PYI_NATIVE_OVERLAY}"
  fi
  if [[ -n "${PYI_ARCH}" ]]; then
    export ARCHITECTOS_PYI_TARGET_ARCH="${PYI_ARCH}"
  else
    unset ARCHITECTOS_PYI_TARGET_ARCH || true
  fi

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
  # Convenience copy without triple only for the host arch (local runs).
  if [[ "${TARGET_TRIPLE}" == "${HOST_TRIPLE}" ]]; then
    cp -f "${SIDECAR_SRC}" "${BINARIES_DIR}/architectos-server"
    chmod +x "${BINARIES_DIR}/architectos-server"
  fi
  echo "    Sidecar → ${SIDECAR_DEST}"
  file "${SIDECAR_DEST}" || true

  echo "==> Installing Tauri CLI deps (desktop/)"
  (
    cd "${DESKTOP_DIR}"
    if [[ ! -d node_modules/@tauri-apps/cli ]]; then
      npm install
    fi
  )

  echo "==> cargo tauri build (--target ${TARGET_TRIPLE})"
  tauri_ec=0
  (
    cd "${DESKTOP_DIR}"
    if [[ "${PLATFORM}" == "macos" ]]; then
      npm run build -- --target "${TARGET_TRIPLE}" --bundles dmg
    else
      npm run build
    fi
  ) || tauri_ec=$?

  cargo_target_root="${CARGO_TARGET_DIR:-${SRC_TAURI}/target}"
  BUNDLE_DIR="${cargo_target_root}/${TARGET_TRIPLE}/release/bundle"
  if [[ ! -d "${BUNDLE_DIR}" ]]; then
    BUNDLE_DIR="${cargo_target_root}/release/bundle"
  fi
  if [[ ! -d "${BUNDLE_DIR}" ]]; then
    BUNDLE_DIR="${SRC_TAURI}/target/${TARGET_TRIPLE}/release/bundle"
  fi
  APP_BUNDLE="${BUNDLE_DIR}/macos/ArchitectOS.app"
  mkdir -p "${DIST_DIR}"

  # Tauri's bundle_dmg.sh often deletes the .app after a successful DMG.
  # Prefer the DMG; only require the .app for the hdiutil fallback.
  if [[ "${PLATFORM}" == "macos" ]]; then
    tauri_dmg="$(find "${BUNDLE_DIR}/dmg" -maxdepth 1 -type f -name 'ArchitectOS_*.dmg' ! -name 'rw.*' 2>/dev/null | head -n 1 || true)"
    if [[ -n "${tauri_dmg}" ]]; then
      cp -f "${tauri_dmg}" "${DEST_DMG}"
      echo "    Copied ${DEST_DMG}"
    elif [[ -d "${APP_BUNDLE}" ]]; then
      echo "==> Creating DMG with hdiutil (Tauri dmg step exit=${tauri_ec})"
      STAGE="$(mktemp -d "${TMPDIR:-/tmp}/architectos-dmg.XXXXXX")"
      cleanup_stage() { rm -rf "${STAGE}"; }
      trap cleanup_stage EXIT
      cp -R "${APP_BUNDLE}" "${STAGE}/ArchitectOS.app"
      ln -sf /Applications "${STAGE}/Applications"
      rm -f "${DEST_DMG}"
      hdiutil create -volname "ArchitectOS" -srcfolder "${STAGE}" -ov -format UDZO "${DEST_DMG}"
      trap - EXIT
      cleanup_stage
      echo "    Wrote ${DEST_DMG}"
    else
      die "Tauri build did not produce a DMG or ${APP_BUNDLE} (exit ${tauri_ec})."
    fi
    # Keep the historic unsuffixed name pointing at the host-arch build.
    if [[ "${TARGET_TRIPLE}" == "${HOST_TRIPLE}" ]]; then
      cp -f "${DEST_DMG}" "${DIST_DIR}/ArchitectOS_${VERSION}_macos.dmg"
    fi
  elif [[ "${tauri_ec}" -ne 0 ]]; then
    die "tauri build failed (exit ${tauri_ec})"
  fi

  local copied=0
  if [[ -d "${BUNDLE_DIR}" ]]; then
    while IFS= read -r -d '' artifact; do
      local base dest
      base="$(basename "${artifact}")"
      case "${base}" in
        rw.*) continue ;;
      esac
      dest="${DIST_DIR}/${base}"
      case "${base}" in
        *.dmg)
          dest="${DEST_DMG}"
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

  if [[ -f "${DEST_DMG}" ]]; then
    copied=$((copied + 1))
  fi

  echo "${BUNDLE_DIR}" > "${DIST_DIR}/bundle_path.txt"
  echo "Wrote sidecar + Tauri bundle metadata under ${DIST_DIR}/"
  if [[ "${copied}" -eq 0 ]]; then
    echo "warning: no dmg/msi/exe found under ${BUNDLE_DIR}; check tauri build logs." >&2
    echo "         Unsigned local binaries may still be under ${SRC_TAURI}/target/" >&2
  fi

  echo "==> Desktop build complete (${PLATFORM} / ${TARGET_TRIPLE})"
}

while IFS= read -r triple; do
  [[ -n "${triple}" ]] || continue
  build_one "${triple}"
done < <(triples_from_arch_arg "${ARCH_ARG}")

echo "==> Artifacts in ${DIST_DIR}"
ls -lh "${DIST_DIR}"/*.dmg 2>/dev/null || true
