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
  python scripts/build_share_package.py --platform windows --with-mcp --with-ide
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
# Official CPython embeddable build — used when cross-packing Windows from macOS/Linux.
WINDOWS_PYTHON_VERSION = "3.12.10"
WINDOWS_PYTHON_EMBED_URL = f"https://www.python.org/ftp/python/{WINDOWS_PYTHON_VERSION}/python-{WINDOWS_PYTHON_VERSION}-embed-amd64.zip"
# Keep pins in sync with backend.architectos.vec_runtime.PIP_PACKAGES.
WINDOWS_VECTOR_PACKAGES = ("sqlite-vec>=0.1.6", "numpy>=1.26")


def read_version() -> str:
    try:
        import tomllib
    except ImportError:  # pragma: no cover
        import tomli as tomllib  # type: ignore
    with (ROOT / "pyproject.toml").open("rb") as fh:
        return str(tomllib.load(fh)["project"]["version"])


def is_windows_pe(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(2) == b"MZ"
    except OSError:
        return False


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


def emit_sqlite_vec_build_probe() -> None:
    backend = ROOT / "backend"
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    from architectos.vec_runtime import emit_builder_probe

    print("==> sqlite-vec build probe", flush=True)
    code = emit_builder_probe()
    if code:
        raise SystemExit("sqlite-vec is required for this build (ARCHITECTOS_REQUIRE_SQLITE_VEC=1).")


def build_server_sidecar(work_dir: Path) -> Path:
    if not have_pyinstaller():
        raise SystemExit("PyInstaller is required. Install with: pip install 'pyinstaller>=6.0'")
    emit_sqlite_vec_build_probe()
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


def find_existing_server(plat: str | None = None) -> Path | None:
    if plat == "windows":
        names = ("architectos-server.exe",)
    elif plat in {"macos", "linux"}:
        names = ("architectos-server",)
    else:
        names = ("architectos-server.exe", "architectos-server")
    candidates = (
        [ROOT / "build" / "share-sidecar" / "dist" / n for n in names]
        + [ROOT / "build" / "desktop-sidecar" / "dist" / n for n in names]
        + [ROOT / "desktop" / "src-tauri" / "binaries" / n for n in names]
        + [ROOT / "dist" / "share" / f"stage-{plat or detect_platform()}" / n for n in names]
        + [ROOT / "dist" / "share" / "stage" / n for n in names]
    )
    for path in candidates:
        if not path.is_file():
            continue
        if plat == "windows" and not is_windows_pe(path):
            continue
        if plat in {"macos", "linux"} and is_windows_pe(path):
            continue
        return path
    return None


def copy_python_tree(src: Path, dest: Path) -> None:
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo", ".DS_Store", "tests", "test_*.py")
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, ignore=ignore)


def download_windows_embed(cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    archive = cache_dir / f"python-{WINDOWS_PYTHON_VERSION}-embed-amd64.zip"
    if archive.is_file() and archive.stat().st_size > 1_000_000:
        return archive
    print(f"==> Downloading Windows embeddable CPython {WINDOWS_PYTHON_VERSION}", flush=True)
    tmp = archive.with_suffix(".zip.partial")
    curl = shutil.which("curl")
    if curl:
        subprocess.run([curl, "-fsSL", "-o", str(tmp), WINDOWS_PYTHON_EMBED_URL], check=True)
    else:
        urllib.request.urlretrieve(WINDOWS_PYTHON_EMBED_URL, tmp)
    tmp.replace(archive)
    return archive


def compile_windows_launcher(dest_exe: Path, *, reuse: bool = False) -> Path:
    src = ROOT / "packaging" / "windows_launcher.c"
    if not src.is_file():
        raise SystemExit(f"Missing {src}")
    dest_exe.parent.mkdir(parents=True, exist_ok=True)
    if reuse and dest_exe.is_file() and is_windows_pe(dest_exe):
        print(f"==> Reusing Windows launcher {dest_exe}", flush=True)
        return dest_exe
    mingw = shutil.which("x86_64-w64-mingw32-gcc")
    if mingw:
        cmd = [mingw, "-O2", "-municode", "-o", str(dest_exe), str(src)]
        print(f"==> Compiling Windows launcher with {mingw}", flush=True)
        subprocess.run(cmd, check=True)
        return dest_exe
    docker = shutil.which("docker")
    if docker:
        print("==> Compiling Windows launcher with Docker MinGW", flush=True)
        work = dest_exe.parent / "src"
        if work.exists():
            shutil.rmtree(work)
        work.mkdir(parents=True)
        shutil.copy2(src, work / "windows_launcher.c")
        script = (
            "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq gcc-mingw-w64-x86-64 >/dev/null "
            "&& x86_64-w64-mingw32-gcc -O2 -municode -o /src/architectos-server.exe /src/windows_launcher.c"
        )
        cmd = [
            docker,
            "run",
            "--rm",
            "-v",
            f"{work}:/src",
            "debian:bookworm-slim",
            "bash",
            "-lc",
            script,
        ]
        subprocess.run(cmd, check=True)
        built = work / "architectos-server.exe"
        if not built.is_file():
            raise SystemExit("Docker MinGW did not produce architectos-server.exe")
        shutil.copy2(built, dest_exe)
        return dest_exe
    raise SystemExit(
        "Cannot cross-compile architectos-server.exe from this OS. "
        "Install mingw-w64 (x86_64-w64-mingw32-gcc) or Docker, or build on Windows with PyInstaller."
    )


def write_python_pth(python_dir: Path) -> None:
    pth_candidates = list(python_dir.glob("python*._pth"))
    if not pth_candidates:
        raise SystemExit(f"No python*._pth in embeddable runtime at {python_dir}")
    stdlib = next((p.name for p in python_dir.glob("python*.zip")), "python312.zip")
    pth_candidates[0].write_text(
        f"{stdlib}\n.\nLib/site-packages\n../backend\nimport site\n",
        encoding="ascii",
    )


def vendor_windows_vector_wheels(python_dir: Path, work_dir: Path) -> Path:
    """Download Windows wheels and unpack them into the embeddable site-packages.

    Embeddable CPython has no pip, so ``python -m pip install sqlite-vec`` cannot
    run on the target. Cross-pack from macOS/Linux uses ``pip download`` with
    Windows tags, then extracts the wheels next to python.exe.
    """
    wheels_dir = work_dir / "win-wheels"
    if wheels_dir.exists():
        shutil.rmtree(wheels_dir)
    wheels_dir.mkdir(parents=True)
    cmd = [
        sys.executable,
        "-m",
        "pip",
        "download",
        "--dest",
        str(wheels_dir),
        "--only-binary=:all:",
        "--python-version",
        "3.12",
        "--platform",
        "win_amd64",
        "--implementation",
        "cp",
        "--abi",
        "cp312",
        *WINDOWS_VECTOR_PACKAGES,
    ]
    print("==> Downloading Windows sqlite-vec / numpy wheels", flush=True)
    subprocess.run(cmd, check=True)
    site = python_dir / "Lib" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    wheels = sorted(wheels_dir.glob("*.whl"))
    if not wheels:
        raise SystemExit("pip download produced no Windows wheels for sqlite-vec/numpy")
    for wheel in wheels:
        print(f"==> Vendoring {wheel.name} into embed site-packages", flush=True)
        with zipfile.ZipFile(wheel) as zf:
            zf.extractall(site)
    return site


def build_windows_portable(stage: Path, work_dir: Path, *, skip_compile: bool = False) -> Path:
    """Cross-pack a Windows Full zip: embeddable CPython + MinGW launcher + frontend/backend."""
    print("==> Building Windows portable runtime (embeddable CPython)", flush=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    embed_zip = download_windows_embed(work_dir / "embed-cache")
    python_dir = stage / "runtime" / "python"
    if python_dir.exists():
        shutil.rmtree(python_dir)
    python_dir.mkdir(parents=True)
    with zipfile.ZipFile(embed_zip) as zf:
        zf.extractall(python_dir)
    write_python_pth(python_dir)
    vendor_windows_vector_wheels(python_dir, work_dir)
    copy_python_tree(ROOT / "backend", stage / "runtime" / "backend")
    copy_python_tree(ROOT / "frontend", stage / "runtime" / "frontend")
    launcher = compile_windows_launcher(work_dir / "architectos-server.exe", reuse=skip_compile)
    dest = stage / "architectos-server.exe"
    shutil.copy2(launcher, dest)
    return dest


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_windows_launcher_bat(path: Path, ps1_name: str, ok_label: str, fail_label: str) -> None:
    """cmd wrapper: pause on Explorer double-click, not when already in a terminal."""
    write_text(
        path,
        (
            "@echo off\r\n"
            f'powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0{ps1_name}" %*\r\n'
            "set ERR=%ERRORLEVEL%\r\n"
            "if %ERR% neq 0 (\r\n"
            "  echo.\r\n"
            f"  echo {fail_label}  exit %ERR%\r\n"
            ") else (\r\n"
            "  echo.\r\n"
            f"  echo {ok_label}\r\n"
            ")\r\n"
            "echo.\r\n"
            'echo %cmdcmdline% | find /i "%~0" >nul\r\n'
            "if %errorlevel%==0 pause\r\n"
            "exit /b %ERR%\r\n"
        ),
    )


def copy_autostart_scripts(stage: Path, plat: str) -> None:
    if plat == "windows":
        shutil.copy2(SCRIPTS / "install_autostart.ps1", stage / "install.ps1")
        shutil.copy2(SCRIPTS / "uninstall_autostart.ps1", stage / "uninstall.ps1")
        write_windows_launcher_bat(
            stage / "install.bat",
            "install.ps1",
            "INSTALL OK",
            "INSTALL FAILED",
        )
        write_windows_launcher_bat(
            stage / "uninstall.bat",
            "uninstall.ps1",
            "UNINSTALL OK",
            "UNINSTALL FAILED",
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
3. Откройте http://127.0.0.1:8766/

Установка (Windows)
-------------------
1. Распакуйте архив.
2. Дважды щёлкните install.bat (или: powershell -File .\\install.ps1)
3. Откройте http://127.0.0.1:8766/

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
Open http://127.0.0.1:8766/ — full guide in docs/GUIDE_RU.md.
"""


def stage_optional_mcp(stage: Path, plat: str, launcher: Path | None = None) -> bool:
    mcp_dir = ROOT / "dist" / "mcp"
    names = ("architectos-mcp.exe", "architectos-mcp") if plat == "windows" else ("architectos-mcp", "architectos-mcp.exe")
    for name in names:
        src = mcp_dir / name
        if src.is_file() and (plat != "windows" or is_windows_pe(src)):
            shutil.copy2(src, stage / name)
            readme = mcp_dir / "README-mcp.txt"
            if readme.is_file():
                shutil.copy2(readme, stage / "README-mcp.txt")
            return True
    if plat == "windows" and launcher is not None and launcher.is_file():
        shutil.copy2(launcher, stage / "architectos-mcp.exe")
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
            if not path.is_file():
                continue
            rel = path.relative_to(stage).as_posix()
            arcname = f"{prefix}/{rel}"
            info = zipfile.ZipInfo.from_file(path, arcname)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3  # Unix, so unzip restores +x
            mode = path.stat().st_mode
            if rel in {"install.sh", "uninstall.sh"} or rel.startswith("architectos-"):
                mode |= 0o111
            info.external_attr = (mode & 0xFFFF) << 16
            zf.writestr(info, path.read_bytes())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build ArchitectOS Full share package with OS autostart.")
    parser.add_argument("--skip-build", action="store_true", help="Reuse an existing architectos-server binary.")
    parser.add_argument("--with-mcp", action="store_true", help="Include dist/mcp binary if present.")
    parser.add_argument("--with-ide", action="store_true", help="Include IDE plugin zip if present.")
    parser.add_argument(
        "--platform",
        choices=("auto", "macos", "windows", "linux"),
        default="auto",
        help="Target OS for the zip (default: auto = this machine). Use windows on macOS/Linux to cross-pack.",
    )
    parser.add_argument(
        "--output-dir",
        default="",
        help="Output directory (default: dist/share).",
    )
    args = parser.parse_args(argv)

    version = read_version()
    plat = detect_platform() if args.platform == "auto" else args.platform
    out_dir = Path(args.output_dir) if args.output_dir else ROOT / "dist" / "share"
    out_dir.mkdir(parents=True, exist_ok=True)
    stage = out_dir / f"stage-{plat}"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    cross_windows = plat == "windows" and os.name != "nt"
    if cross_windows:
        server = build_windows_portable(
            stage,
            ROOT / "build" / "windows-share",
            skip_compile=bool(args.skip_build),
        )
        exe_name = "architectos-server.exe"
    else:
        work = ROOT / "build" / "share-sidecar"
        if args.skip_build:
            server = find_existing_server(plat)
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
        server = dest_server

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
        included_mcp = stage_optional_mcp(stage, plat, launcher=server if plat == "windows" else None)
        if not included_mcp:
            print("warning: --with-mcp set but no Windows MCP launcher could be staged; skipping.", file=sys.stderr)
    if args.with_ide:
        included_ide = stage_optional_ide(stage)
        if not included_ide:
            print("warning: --with-ide set but IDE zip not found; skipping.", file=sys.stderr)

    archive = out_dir / f"ArchitectOS_Full_{version}_{plat}.zip"
    build_archive(stage, archive)

    print(f"Wrote {archive}")
    print(f"  server={exe_name} mcp={included_mcp} ide={included_ide} platform={plat}")
    print("Recipients: unpack → run install.sh / install.bat → open http://127.0.0.1:8766/")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
