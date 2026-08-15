from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from backend.architectos.project_files import delete_project_file, save_project_file


class ProjectFilesTests(unittest.TestCase):
    def test_save_and_delete_through_symlinked_root(self) -> None:
        if os.name == "nt":
            self.skipTest("symlink creation needs privileges on Windows")
        with tempfile.TemporaryDirectory() as tmp:
            real_root = Path(tmp) / "real"
            real_root.mkdir()
            link_root = Path(tmp) / "link"
            os.symlink(real_root, link_root)
            saved = save_project_file(link_root, "p1", "docs/note.txt", "hello")
            self.assertTrue(saved["success"])
            self.assertEqual(saved["path"], str(Path("docs") / "note.txt"))
            self.assertEqual((real_root / "docs" / "note.txt").read_text(encoding="utf-8"), "hello")
            deleted = delete_project_file(link_root, "p1", "docs/note.txt")
            self.assertTrue(deleted["success"])
            self.assertEqual(deleted["path"], str(Path("docs") / "note.txt"))
            self.assertFalse((real_root / "docs" / "note.txt").exists())

    def test_path_traversal_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(ValueError):
                save_project_file(root, "p1", "../evil.txt", "x")
            self.assertFalse((root.parent / "evil.txt").exists())
            with self.assertRaises(ValueError):
                delete_project_file(root, "p1", "../evil.txt")

    def test_symlink_inside_project_pointing_outside_rejected(self) -> None:
        if os.name == "nt":
            self.skipTest("symlink creation needs privileges on Windows")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            root.mkdir()
            outside = Path(tmp) / "outside"
            outside.mkdir()
            os.symlink(outside, root / "sneaky")
            with self.assertRaises(ValueError):
                save_project_file(root, "p1", "sneaky/evil.txt", "x")
            self.assertFalse((outside / "evil.txt").exists())


if __name__ == "__main__":
    unittest.main()
