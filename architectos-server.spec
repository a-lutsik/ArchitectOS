# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller one-file build for the ArchitectOS desktop server sidecar.

Embeds ``backend/`` (via imports) and ``frontend/`` static files.
Build via scripts/build_desktop.sh or scripts/build_desktop.ps1.

sqlite-vec is collected when the builder Python can import it. Loadable SQLite
extensions must also be enabled in that Python (Homebrew/conda, not python.org
macOS) or the frozen app keeps the in-memory Python vector fallback.

Cross-arch macOS: set ``ARCHITECTOS_PYI_TARGET_ARCH`` to ``x86_64`` or
``arm64``. Optionally set ``ARCHITECTOS_SQLITE_VEC_DYLIB`` to a matching
``vec0.dylib`` (needed when the host wheel is the other architecture).
"""

import os
import subprocess
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve()
hiddenimports = collect_submodules("architectos")
datas = [(str(ROOT / "frontend"), "frontend")]
binaries = []

_raw_arch = (os.environ.get("ARCHITECTOS_PYI_TARGET_ARCH") or "").strip()
TARGET_ARCH = _raw_arch if _raw_arch in ("x86_64", "arm64", "universal2") else None
SQLITE_VEC_DYLIB = (os.environ.get("ARCHITECTOS_SQLITE_VEC_DYLIB") or "").strip()


def _dylib_arches(path: str) -> set[str]:
    try:
        out = subprocess.check_output(["lipo", "-archs", path], text=True).strip()
        return set(out.split())
    except Exception:
        return set()


def _dylib_ok_for_target(path: str, arch: str | None) -> bool:
    if not arch or arch == "universal2":
        return True
    found = _dylib_arches(path)
    if not found:
        return True
    if arch in found:
        return True
    return "x86_64" in found and "arm64" in found


try:
    import sqlite_vec
    from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

    hiddenimports.append("sqlite_vec")
    datas += collect_data_files("sqlite_vec")
    binaries += [
        item
        for item in collect_dynamic_libs("sqlite_vec")
        if _dylib_ok_for_target(item[0], TARGET_ARCH)
    ]
    del sqlite_vec
except Exception:
    pass

if SQLITE_VEC_DYLIB:
    binaries.append((SQLITE_VEC_DYLIB, "sqlite_vec"))


def _rewrite_binaries_for_arch(toc, overlay: Path, arch: str):
    index: dict[str, Path] = {}
    if overlay.is_dir():
        for path in overlay.rglob("*"):
            if path.suffix in {".so", ".dylib"}:
                index[path.name] = path
                index[str(path.relative_to(overlay)).replace("\\", "/")] = path
    rewritten = []
    missing = []
    for dest, src, kind in toc:
        src_s = str(src)
        if _dylib_ok_for_target(src_s, arch):
            rewritten.append((dest, src, kind))
            continue
        dest_key = str(dest).replace("\\", "/")
        replacement = index.get(dest_key) or index.get(Path(dest_key).name)
        if replacement is not None and _dylib_ok_for_target(str(replacement), arch):
            rewritten.append((dest, str(replacement), kind))
            continue
        missing.append(f"{dest} <- {src_s}")
    if missing:
        raise SystemExit(
            "No "
            + arch
            + " native replacement for:\n  "
            + "\n  ".join(missing)
            + "\nSet ARCHITECTOS_PYI_NATIVE_OVERLAY to extracted arm64/universal2 wheels."
        )
    return rewritten


a = Analysis(
    [str(ROOT / "packaging" / "architectos_server_main.py")],
    pathex=[str(ROOT / "backend")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)

_overlay = Path(os.environ.get("ARCHITECTOS_PYI_NATIVE_OVERLAY") or "")
if TARGET_ARCH:
    a.binaries = _rewrite_binaries_for_arch(a.binaries, _overlay, TARGET_ARCH)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="architectos-server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=TARGET_ARCH,
    codesign_identity=None,
    entitlements_file=None,
)
