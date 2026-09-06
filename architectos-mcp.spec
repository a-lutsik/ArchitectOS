# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller one-file build for the ArchitectOS MCP memory server.

Entry: mcp_memory_server.py → architectos.mcp_server:main (console script architectos-mcp).
Build via scripts/build_mcp.sh or scripts/build_mcp.ps1.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH)
hiddenimports = collect_submodules("architectos")
datas = []
binaries = []
try:
    import sqlite_vec
    from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

    hiddenimports.append("sqlite_vec")
    datas += collect_data_files("sqlite_vec")
    binaries += collect_dynamic_libs("sqlite_vec")
    del sqlite_vec
except Exception:
    pass

a = Analysis(
    [str(ROOT / "mcp_memory_server.py")],
    pathex=[str(ROOT / "backend")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # Optional accelerations — ArchitectOS falls back to pure stdlib without them.
    excludes=[
        "numpy",
        "fastembed",
        "sentence_transformers",
        "torch",
        "tensorflow",
        "sklearn",
        "scipy",
    ],
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
    name="architectos-mcp",
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
