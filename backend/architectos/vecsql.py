"""Optional sqlite-vec acceleration for memory embeddings.

Runtime stays stdlib-only: if ``sqlite-vec`` is missing or the local SQLite
cannot load extensions, callers keep the existing Python / numpy path.
Loaded per connection — SQLite extensions do not persist across connects.
"""

from __future__ import annotations

import logging
import sqlite3
import struct
from typing import Any

_LOG = logging.getLogger("architectos.vecsql")
_PROBE: bool | None = None
_PROBE_REASON = "not-probed"


def sqlite_vec_importable() -> bool:
    try:
        import sqlite_vec  # noqa: F401
    except Exception:  # noqa: BLE001 - optional extra
        return False
    return True


def sqlite_load_extension_supported() -> bool:
    """True when this process's sqlite3 can call enable_load_extension."""
    try:
        conn = sqlite3.connect(":memory:")
        try:
            return hasattr(conn, "enable_load_extension")
        finally:
            conn.close()
    except Exception:  # noqa: BLE001
        return False


def sqlite_vec_status() -> dict[str, Any]:
    import sys

    from .paths import is_frozen

    available = sqlite_vec_available()
    return {
        "installed": sqlite_vec_importable(),
        "loaded": available,
        "reason": _PROBE_REASON,
        "load_extension": sqlite_load_extension_supported(),
        "python": sys.executable,
        "version": sys.version.split()[0],
        "frozen": is_frozen(),
    }


def reset_sqlite_vec_probe() -> None:
    global _PROBE, _PROBE_REASON
    _PROBE = None
    _PROBE_REASON = "not-probed"


def sqlite_vec_available() -> bool:
    """True when the extension loads and ``vec_distance_cosine`` exists."""
    global _PROBE, _PROBE_REASON
    if _PROBE is not None:
        return _PROBE
    if not sqlite_load_extension_supported():
        _PROBE = False
        _PROBE_REASON = "load-extension-disabled"
        return False
    if not sqlite_vec_importable():
        _PROBE = False
        _PROBE_REASON = "not-installed"
        return False
    try:
        conn = sqlite3.connect(":memory:")
        try:
            _PROBE = load_sqlite_vec(conn)
            _PROBE_REASON = "ok" if _PROBE else "probe-failed"
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 - probe must never break storage
        _LOG.debug("sqlite-vec probe failed: %s", exc)
        _PROBE = False
        _PROBE_REASON = "probe-failed"
    return bool(_PROBE)


def load_sqlite_vec(conn: sqlite3.Connection) -> bool:
    """Load sqlite-vec onto ``conn``. Safe to call repeatedly. Returns success."""
    try:
        import sqlite_vec
    except Exception:
        return False
    try:
        conn.enable_load_extension(True)
        try:
            sqlite_vec.load(conn)
        finally:
            try:
                conn.enable_load_extension(False)
            except sqlite3.Error:
                pass
        conn.execute("SELECT vec_distance_cosine(?, ?)", (struct.pack("2f", 1.0, 0.0), struct.pack("2f", 1.0, 0.0))).fetchone()
        return True
    except Exception as exc:  # noqa: BLE001 - missing load_extension / ABI
        _LOG.debug("sqlite-vec not loaded: %s", exc)
        return False


def cosine_distance_to_similarity(distance: Any) -> float:
    """sqlite-vec cosine distance is 1 - cosine similarity for unit vectors."""
    try:
        return 1.0 - float(distance)
    except (TypeError, ValueError):
        return 0.0
