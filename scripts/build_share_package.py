#!/usr/bin/env python3
"""Build a shareable ArchitectOS Full package with OS autostart installers.

Produces dist/share/ArchitectOS_Full_<ver>_<platform>.zip containing:
  - architectos-server binary (PyInstaller)
  - install / uninstall scripts (register login/boot autostart)
  - optional architectos-mcp + IDE plugin zip if already built
  - Russian user guide (docs/GUIDE_RU.md)
  - short README-SHARE (RU + EN)

Examples:
  python scripts/build_share_package.py
  python scripts/build_share_package.py --skip-build   # reuse existing sidecar
  python scripts/build_share_package.py --with-mcp --with-ide
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def read_version() -> str:
    try:
        import tomllib
    except ImportError:  # pragma: no cover
        import tomli as tomllib  # type: ignore
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return str(tomllib.load(fh)["project"]["version"])


def detect_platform() -> str:
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    if system == "windows":
        return "windows"
    if system == "linux":
        return "linux"
    return system


def have_pyinstaller() -> bool:
    if shutil.which("pyinstaller"):
        return True
    try:
        subprocess.run(
            [sys.executable, "-c", "import PyInstaller"],
            check=True,
            capture_output=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def build_server_sidecar(work_dir: Path) -> Path:
    if not have_pyinstaller():
        raise SystemExit(
            "PyInstaller is required. Install with: pip install 'pyinstaller>=6.0'"
        )
    work_dir.mkdir(parents=True, exist_ok=True)
    pyi = ["pyinstaller"] if shutil.which("pyinstaller") else [sys.executable, "-m", "PyInstaller"]
    cmd = pyi + [
        "--noconfirm",
        "--clean",
        "--distpath",
        str(work_dir / "dist"),
        "--workpath",
        str(work_dir / "work"),
        str(ROOT / "architectos-server.spec"),
    ]
    print("==> Building architectos-server sidecar", flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)
    exe_name = "architectos-server.exe" if os.name == "nt" else "architectos-server"
    path = work_dir / "dist" / exe_name
    if not path.is_file():
        # one-file may omit .exe in name on some hosts
        alt = work_dir / "dist" / "architectos-server"
        if alt.is_file():
            return alt
        raise SystemExit(f"PyInstaller did not produce {path}")
    return path


def find_existing_server() -> Path | None:
    names = ("architectos-server.exe", "architectos-server")
    candidates = [
        ROOT / "dist" / "share" / "stage" / n for n in names
    ] + [
        ROOT / "desktop" / "src-tauri" / "binaries" / n for n in names
    ] + [
        ROOT / "build" / "desktop-sidecar" / "dist" / n for n in names
    ] + [
        ROOT / "build" / "share-sidecar" / "dist" / n for n in names
    ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def copy_autostart_scripts(stage: Path, plat: str) -> None:
    if plat == "windows":
        shutil.copy2(SCRIPTS / "install_autostart.ps1", stage / "install.ps1")
        shutil.copy2(SCRIPTS / "uninstall_autostart.ps1", stage / "uninstall.ps1")
        write_text(
            stage / "install.bat",
            "@echo off\r\n"
            "powershell -NoProfile -ExecutionPolicy Bypass -File \"%~dp0install.ps1\" %*\r\n"
            "if errorlevel 1 pause\r\n",
        )
        write_text(
            stage / "uninstall.bat",
            "@echo off\r\n"
            "powershell -NoProfile -ExecutionPolicy Bypass -File \"%~dp0uninstall.ps1\" %*\r\n"
            "if errorlevel 1 pause\r\n",
        )
    else:
        for src_name, dest_name in (
            ("install_autostart.sh", "install.sh"),
            ("uninstall_autostart.sh", "uninstall.sh"),
        ):
            dest = stage / dest_name
            shutil.copy2(SCRIPTS / src_name, dest)
            dest.chmod(dest.stat().st_mode | 0o111)


README_SHARE = """ArchitectOS Full — пакет для передачи коллегам
================================================

Что внутри
----------
- architectos-server — локальный HTTP-сервер + веб-UI (без установки Python)
- install / uninstall — установка и автозапуск сервера при входе в ОС
- docs/GUIDE_RU.md — полное руководство (Full / MCP / IDE plugin)
- (опционально) architectos-mcp — MCP-сервер памяти для Cursor / Claude
- (опционально) ide/ — zip плагина IntelliJ

Установка (macOS)
-----------------
1. Распакуйте архив.
2. В Finder: правый клик по install.sh → Открыть (первый раз Gatekeeper).
   Или в Terminal:
     chmod +x install.sh uninstall.sh architectos-server
     ./install.sh
3. Откройте http://127.0.0.1:8765/

Установка (Windows)
-------------------
1. Распакуйте архив.
2. Дважды щёлкните install.bat (или: powershell -File .\\install.ps1)
3. Откройте http://127.0.0.1:8765/

Автозапуск
----------
После install сервер стартует при входе в систему:
  macOS  — LaunchAgent com.architectos.server
  Windows — Scheduled Task «ArchitectOS Server» (+ HKCU Run)

Данные: ~/ArchitectOS  или  %LOCALAPPDATA%\\ArchitectOS
Отмена: ./uninstall.sh  /  uninstall.bat

Подробнее: docs/GUIDE_RU.md

English
-------
Unpack, run install.sh (macOS) or install.bat (Windows). This copies
architectos-server into ARCHITECTOS_ROOT/bin and registers OS login autostart.
Open http://127.0.0.1:8765/ — full guide in docs/GUIDE_RU.md.
"""


def stage_optional_mcp(stage: Path) -> bool:
    mcp_dir = ROOT / "dist" / "mcp"
    names = ("architectos-mcp.exe", "architectos-mcp")
    for name in names:
        src = mcp_dir / name
        if src.is_file():
            shutil.copy2(src, stage / name)
            readme = mcp_dir / "README-mcp.txt"
            if readme.is_file():
                shutil.copy2(readme, stage / "README-mcp.txt")
            return True
    return False


def stage_optional_ide(stage: Path) -> bool:
    ide_dir = ROOT / "dist" / "ide"
    if not ide_dir.is_dir():
        # fall back to gradle output
        distros = list((ROOT / "ide" / "intellij" / "build" / "distributions").glob("*.zip"))
        if not distros:
            return False
        dest_dir = stage / "ide"
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sorted(distros)[-1], dest_dir / sorted(distros)[-1].name)
        return True
    zips = sorted(ide_dir.glob("ArchitectOS-Memory-*.zip")) + sorted(ide_dir.glob("*.zip"))
    if not zips:
        return False
    dest_dir = stage / "ide"
    dest_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(zips[-1], dest_dir / zips[-1].name)
    return True


def build_archive(stage: Path, archive: Path) -> None:
    if archive.exists():
        archive.unlink()
    prefix = archive.stem  # ArchitectOS_Full_1.0.0_macos
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(stage.rglob("*")):
            if path.is_file():
                arcname = f"{prefix}/{path.relative_to(stage).as_posix()}"
                zf.write(path, arcname)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build ArchitectOS Full share package with OS autostart.")
    parser.add_argument("--skip-build", action="store_true", help="Reuse an existing architectos-server binary.")
    parser.add_argument("--with-mcp", action="store_true", help="Include dist/mcp binary if present.")
    parser.add_argument("--with-ide", action="store_true", help="Include IDE plugin zip if present.")
    parser.add_argument(
        "--output-dir",
        default="",
        help="Output directory (default: dist/share).",
    )
    args = parser.parse_args(argv)

    version = read_version()
    plat = detect_platform()
    out_dir = Path(args.output_dir) if args.output_dir else ROOT / "dist" / "share"
    out_dir.mkdir(parents=True, exist_ok=True)
    stage = out_dir / "stage"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    work = ROOT / "build" / "share-sidecar"
    if args.skip_build:
        server = find_existing_server()
        if server is None:
            raise SystemExit("No existing architectos-server found; omit --skip-build to build one.")
        print(f"==> Reusing sidecar {server}", flush=True)
    else:
        server = build_server_sidecar(work)

    exe_name = "architectos-server.exe" if plat == "windows" or server.suffix.lower() == ".exe" else "architectos-server"
    dest_server = stage / exe_name
    shutil.copy2(server, dest_server)
    if plat != "windows":
        dest_server.chmod(dest_server.stat().st_mode | 0o111)

    copy_autostart_scripts(stage, plat)

    guide_src = ROOT / "docs" / "GUIDE_RU.md"
    if not guide_src.is_file():
        raise SystemExit("Missing docs/GUIDE_RU.md — create the Russian guide before packaging.")
    docs_dir = stage / "docs"
    docs_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(guide_src, docs_dir / "GUIDE_RU.md")
    # Also copy installers overview if present
    for extra in ("INSTALLERS.md", "MCP_MEMORY_SERVER.md"):
        src = ROOT / "docs" / extra
        if src.is_file():
            shutil.copy2(src, docs_dir / extra)

    write_text(stage / "README-SHARE.txt", README_SHARE)
    write_text(stage / "ЧИТАЙМЕНЯ.txt", README_SHARE)

    included_mcp = False
    included_ide = False
    if args.with_mcp:
        included_mcp = stage_optional_mcp(stage)
        if not included_mcp:
            print("warning: --with-mcp set but dist/mcp binary not found; skipping.", file=sys.stderr)
    if args.with_ide:
        included_ide = stage_optional_ide(stage)
        if not included_ide:
            print("warning: --with-ide set but IDE zip not found; skipping.", file=sys.stderr)

    archive = out_dir / f"ArchitectOS_Full_{version}_{plat}.zip"
    build_archive(stage, archive)

    print(f"Wrote {archive}")
    print(f"  server={exe_name} mcp={included_mcp} ide={included_ide}")
    print("Recipients: unpack → run install.sh / install.bat → open http://127.0.0.1:8765/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
