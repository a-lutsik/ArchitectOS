from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from backend.architectos.paths import (
    default_architectos_root,
    resolve_frontend_root,
    resolve_project_root,
)


class PathsTests(unittest.TestCase):
    def test_architechos_root_override(self) -> None:
        with mock.patch.dict(os.environ, {"ARCHITECTOS_ROOT": "~/ArchitectOS-Test-Override"}, clear=False):
            root = resolve_project_root()
        self.assertEqual(root, Path("~/ArchitectOS-Test-Override").expanduser().resolve())

    def test_default_packaged_root_unix_shape(self) -> None:
        if os.name == "nt":
            self.skipTest("unix default")
        self.assertEqual(default_architectos_root(), Path.home() / "ArchitectOS")

    def test_frontend_override(self) -> None:
        fake = Path.cwd()
        with mock.patch.dict(os.environ, {"ARCHITECTOS_FRONTEND": str(fake)}, clear=False):
            self.assertEqual(resolve_frontend_root(), fake.resolve())

    def test_source_checkout_resolves_repo(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ARCHITECTOS_ROOT", None)
            root = resolve_project_root()
        self.assertTrue((root / "run_architectos.py").exists() or (root / "frontend").is_dir())


if __name__ == "__main__":
    unittest.main()
