from __future__ import annotations

import json
import re
import zipfile
from pathlib import Path
from typing import Any

RELEASE_NAME = "architectos"
RELEASE_VERSION = "1.0.0"
EXCLUDED_PARTS = {".git", "__pycache__", ".pytest_cache", "node_modules", "data", "memory", "backups", "dist", ".playwright-mcp", ".idea"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".db", ".sqlite", ".zip", ".png"}
EXCLUDED_ROOT_PREFIXES = ("fix-", "sprint2-", "shared-", "backlog-", "01-", "02-", "03-", "04-", "05-", "06-", "07-", "08-", "09-", "10-", "11-", "12-", "13-", "14-")
REQUIRED_FILES = [
    "README.md",
    "package.json",
    "run_architectos.py",
    "start-architectos.ps1",
    "start-architectos.bat",
    "start-architectos-app.ps1",
    "start-architectos-app.bat",
    "backend/app.py",
    "backend/architectos/service.py",
    "backend/architectos/server.py",
    "backend/architectos/storage.py",
    "backend/architectos/adapters.py",
    "backend/architectos/adapters_base.py",
    "backend/architectos/adapters_cli.py",
    "backend/architectos/config.py",
    "backend/architectos/security.py",
    "backend/architectos/launcher.py",
    "backend/architectos/release.py",
    "frontend/index.html",
    "frontend/app.js",
    "frontend/styles.css",
    "docs/STARTUP.md",
    "docs/CONFIGURATION.md",
    "docs/PRODUCTION.md",
    "docs/RELEASE_QA.md",
    "docs/ROADMAP_CHECKLIST.md",
    "docs/GUIDE_RU.md",
    "scripts/install_autostart.sh",
    "scripts/install_autostart.ps1",
    "scripts/uninstall_autostart.sh",
    "scripts/uninstall_autostart.ps1",
    "scripts/build_share_package.py",
    "tests/test_memory.py",
    "tests/test_e2e.py",
    "tests/test_fake_adapters.py",
    "tests/test_ops.py",
    "tests/test_release.py",
]


def should_include(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    if any(part in EXCLUDED_PARTS for part in relative.parts):
        return False
    if path.suffix.lower() in EXCLUDED_SUFFIXES:
        return False
    # Never ship local secret files in a release archive.
    if relative.name == ".env" or (relative.name.startswith(".env.") and relative.name not in {".env.example", ".env.sample", ".env.template"}):
        return False
    if len(relative.parts) == 1 and relative.name.startswith(EXCLUDED_ROOT_PREFIXES):
        return False
    if relative.name.startswith("index.html.backup"):
        return False
    if len(relative.parts) == 1 and relative.suffix.lower() == ".md" and relative.name.endswith("_COMPLETE.md"):
        return False
    if len(relative.parts) == 1 and relative.name.endswith("_preview.html"):
        return False
    return path.is_file()


def iter_release_files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if should_include(path, root))


def visual_qa_checks(root: Path) -> dict[str, bool]:
    css_path = root / "frontend" / "styles.css"
    html_path = root / "frontend" / "index.html"
    app_path = root / "frontend" / "app.js"
    if not css_path.exists() or not html_path.exists() or not app_path.exists():
        return {
            "desktop_grid": False,
            "tablet_breakpoint": False,
            "mobile_breakpoint": False,
            "mobile_single_column": False,
            "graph_canvas": False,
            "graph_mobile_layout": False,
            "text_overflow_guards": False,
            "no_negative_letter_spacing": False,
            "no_viewport_font_scaling": False,
        }
    css = css_path.read_text(encoding="utf-8")
    html = html_path.read_text(encoding="utf-8")
    frontend_js = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((root / "frontend").glob("*.js"))
    )
    # Normalize whitespace so checks are robust to formatting (expanded vs minified CSS).
    compact = "".join(css.split())
    has_desktop_grid = ".dashboard-grid" in css and (
        "grid-template-columns:var(--sidebar-w)1fr" in compact
        or "grid-template-columns:238px1fr" in compact
        or "grid-template-columns:250px1fr" in compact
    )
    return {
        "desktop_grid": has_desktop_grid,
        "tablet_breakpoint": "@media(max-width:1100px)" in compact,
        "mobile_breakpoint": "@media(max-width:760px)" in compact,
        "mobile_single_column": ".app-shell{grid-template-columns:1fr" in compact and ".task-board{grid-template-columns:1fr" in compact,
        "graph_canvas": '<canvas id="graph-canvas"' in html and "resizeGraphCanvas" in frontend_js,
        "graph_mobile_layout": ".graph-layout{display:grid" in compact and "grid-auto-rows:640px" in compact,
        "text_overflow_guards": "overflow-wrap:anywhere" in compact and "min-width:0" in compact,
        # Tight heading tracking (≈ -0.02em) is deliberate; flag only aggressive negatives.
        "no_negative_letter_spacing": re.search(r"letter-spacing:-(0\.0[5-9]|0\.[1-9]|[1-9])", compact) is None,
        # Viewport units are fine for widths; fonts must not scale with the viewport.
        "no_viewport_font_scaling": "font-size:" in compact and re.search(r"font-size:[^;}]*\dvw", compact) is None,
    }


def release_manifest(root: Path) -> dict[str, Any]:
    root = root.resolve()
    files = [path.relative_to(root).as_posix() for path in iter_release_files(root)]
    required = {item: item in files for item in REQUIRED_FILES}
    visual = visual_qa_checks(root)
    checks = {
        "required_files": all(required.values()),
        "no_runtime_data": not any(item.startswith("data/") or item.startswith("memory/") for item in files),
        "no_cache_files": not any("__pycache__" in item or item.endswith(".pyc") for item in files),
        "startup_scripts": all(item in files for item in ["run_architectos.py", "start-architectos.ps1", "start-architectos.bat", "start-architectos-app.ps1", "start-architectos-app.bat"]),
        "configuration_docs": all(item in files for item in ["docs/STARTUP.md", "docs/CONFIGURATION.md", "docs/RELEASE_QA.md"]),
        "share_autostart": all(
            item in files
            for item in [
                "docs/GUIDE_RU.md",
                "scripts/install_autostart.sh",
                "scripts/install_autostart.ps1",
                "scripts/uninstall_autostart.sh",
                "scripts/uninstall_autostart.ps1",
                "scripts/build_share_package.py",
            ]
        ),
        "visual_qa": all(visual.values()),
    }
    return {
        "name": RELEASE_NAME,
        "version": RELEASE_VERSION,
        "root": str(root),
        "file_count": len(files),
        "files": files,
        "required": required,
        "checks": checks,
        "visual_qa": visual,
        "ready": all(checks.values()),
    }


def build_release_archive(root: Path, output_dir: Path | None = None) -> dict[str, Any]:
    root = root.resolve()
    manifest = release_manifest(root)
    if not manifest["ready"]:
        failed = [name for name, passed in manifest["checks"].items() if not passed]
        raise RuntimeError(f"release manifest is not ready: {', '.join(failed)}")
    output_dir = (output_dir or root / "dist").resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / f"{RELEASE_NAME}-{RELEASE_VERSION}.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in iter_release_files(root):
            zf.write(path, f"{RELEASE_NAME}-{RELEASE_VERSION}/{path.relative_to(root).as_posix()}")
        zf.writestr(f"{RELEASE_NAME}-{RELEASE_VERSION}/release-manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
    payload = dict(manifest)
    payload["archive"] = str(archive)
    payload["archive_size"] = archive.stat().st_size
    return payload
