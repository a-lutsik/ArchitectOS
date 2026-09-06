from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "backend"))

from architectos.agent_hooks import main

if __name__ == "__main__":
    raise SystemExit(main())
