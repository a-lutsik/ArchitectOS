from __future__ import annotations

import json
import subprocess
import unittest
from unittest import mock

from backend.architectos.folder_picker import _run_folder_picker_command, pick_folder_subprocess


class FolderPickerTests(unittest.TestCase):
    def test_run_folder_picker_command_treats_user_cancel_as_cancelled(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["osascript"],
            returncode=1,
            stdout="",
            stderr="execution error: User canceled. (-128)",
        )
        with mock.patch("subprocess.run", return_value=completed):
            result = _run_folder_picker_command(["osascript", "-e", "choose folder"])
        self.assertTrue(result["supported"])
        self.assertTrue(result["cancelled"])
        self.assertEqual(result["path"], "")

    def test_pick_folder_subprocess_parses_worker_json(self) -> None:
        payload = {"supported": True, "cancelled": False, "path": "/tmp/project"}
        completed = subprocess.CompletedProcess(args=["python"], returncode=0, stdout=json.dumps(payload), stderr="")
        with mock.patch("subprocess.run", return_value=completed) as run_mock:
            result = pick_folder_subprocess("/tmp")
        self.assertEqual(result, payload)
        command = run_mock.call_args.args[0]
        self.assertTrue(str(command[1]).endswith("folder_picker.py"))
        self.assertNotIn("-m", command)


if __name__ == "__main__":
    unittest.main()
