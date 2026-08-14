# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller one-file build for the ArchitectOS desktop server sidecar.

Embeds ``backend/`` (via imports) and ``frontend/`` static files.
Build via scripts/build_desktop.sh or scripts/build_desktop.ps1.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parent
hiddenimports = collect_submodules("architectos")

a = Analysis(
    [str(ROOT / "packaging" / "architectos_server_main.py")],
    pathex=[str(ROOT / "backend")],
    binaries=[],
    datas=[(str(ROOT / "frontend"), "frontend")],
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
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
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
