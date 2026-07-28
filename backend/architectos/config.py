from __future__ import annotations

import os

APP_NAME = "ArchitectOS"
APP_VERSION = "1.0.0"
APP_ENV = os.environ.get("ARCHITECTOS_ENV", "local")

DEFAULT_HOST = os.environ.get("ARCHITECTOS_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.environ.get("ARCHITECTOS_PORT", "8765"))
BACKUP_RETENTION = int(os.environ.get("ARCHITECTOS_BACKUP_RETENTION", "10"))
ACCESS_LOG = os.environ.get("ARCHITECTOS_ACCESS_LOG", "").strip().lower() in {"1", "true", "yes", "on"}
