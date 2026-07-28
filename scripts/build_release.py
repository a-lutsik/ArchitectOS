from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from architectos.release import build_release_archive, release_manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build or check the ArchitectOS portable release package.")
    parser.add_argument("--check-only", action="store_true", help="Print the release manifest without writing an archive.")
    parser.add_argument("--output-dir", default="", help="Directory for the release zip. Defaults to ./dist.")
    args = parser.parse_args(argv)

    if args.check_only:
        manifest = release_manifest(ROOT)
    else:
        manifest = build_release_archive(ROOT, Path(args.output_dir) if args.output_dir else None)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0 if manifest.get("ready") else 1


if __name__ == "__main__":
    raise SystemExit(main())
