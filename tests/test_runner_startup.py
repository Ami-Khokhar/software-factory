"""Startup failures retain identity without starting git, Docker, or a coding process."""
import errno
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from factory_mcp import documents
from factory_mcp.config import Config
from tests.fakes import workitem
from worker import runner


class RunnerStartupTest(unittest.TestCase):
    def setUp(self):
        self.order = documents.workorder(workitem(goal="private-workorder-sentinel"), "implement", 0,
                                        Config(mcp_token="t", repos=frozenset()), "base")
        self.text = documents.worker_input(self.order, "factory/1")
        self.args = ["--env", "TEST_SECRET=secret-env-value", "--", "sentinel-harness", "secret-command-argument", self.text]

    def invoke(self):
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(runner.main(self.args), 0)
        lines = output.getvalue().splitlines()
        self.assertEqual(len(lines), 1)
        result = json.loads(lines[0])
        self.assertEqual(result["workOrderId"], self.order["id"])
        self.assertEqual(documents.check_result(result, self.order), [])
        return result

    def test_accessible_directory_reaches_checkout_setup(self):
        """Guard: a valid launch directory continues to the git setup boundary."""
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(runner.os, "getcwd", return_value=directory), \
                    mock.patch.object(runner.subprocess, "run", side_effect=runner.WorkerError("setup seam")) as process:
                result = self.invoke()
            self.assertEqual(process.call_count, 1)
            self.assertEqual(process.call_args.kwargs["cwd"], directory)
            self.assertEqual(process.call_args.args[0][0], "git")
        self.assertEqual(result["error"], "setup seam")

    def test_directory_resolution_errors_are_bounded_and_do_not_start_coding(self):
        for code, error in ((errno.EPERM, PermissionError), (errno.ENOENT, FileNotFoundError)):
            with self.subTest(code=code), \
                    mock.patch.object(runner.os, "getcwd", side_effect=error(code, "secret-env-value", self.text)), \
                    mock.patch.object(runner.subprocess, "run") as process, \
                    mock.patch.object(runner.tempfile, "mkdtemp") as scratch:
                for _ in range(2):
                    result = self.invoke()
                    phase = result["diagnostics"]["phases"][0]
                    self.assertEqual((phase["phase"], phase["exitCode"]), ("preparation", code))
                    self.assertIn(f"errno={code}", result["error"])
                    self.assertLess(len(json.dumps(result)), 2000)
                    for secret in ("secret-env-value", "secret-command-argument", "factory.workorder/v1", "private-workorder-sentinel"):
                        self.assertNotIn(secret, json.dumps(result))
                process.assert_not_called()
                scratch.assert_not_called()

    def test_disappeared_selected_directory_fails_before_setup(self):
        with tempfile.TemporaryDirectory() as parent:
            missing = str(Path(parent) / "missing")
            with mock.patch.object(runner.os, "getcwd", return_value=missing), \
                    mock.patch.object(runner.subprocess, "run") as process:
                result = self.invoke()
                process.assert_not_called()
        self.assertIn("errno=2", result["error"])

    def test_removed_directory_fails_without_a_fallback(self):
        # A real removed cwd in a disposable fixture. Restore by fd even if a check fails.
        previous = os.open(".", os.O_RDONLY)
        with tempfile.TemporaryDirectory() as parent:
            directory = Path(parent) / "removed"
            directory.mkdir()
            try:
                os.chdir(directory)
                directory.rmdir()
                with mock.patch.object(runner.subprocess, "run") as process:
                    result = self.invoke()
                process.assert_not_called()
                self.assertIn("errno=2", result["error"])
            finally:
                os.fchdir(previous)
                os.close(previous)
