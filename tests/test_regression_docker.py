"""scripts/regression_docker.py: the real-container harness is explicit and honest.

No test here runs Docker; they only reach the harness's own logic through its
public interface, faking the process boundaries (`docker info`, `docker
image inspect`, `docker run`) that the harness sits behind. The harness itself
is exercised against real containers by running the script by hand.
"""
import io
import json
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "worker"))
import regression_docker  # noqa: E402
import runner  # noqa: E402


def docker_status(code):
    return mock.patch.object(regression_docker.runner, "docker_status", return_value=code)


def inspect(returncode):
    """A completed `docker image inspect` with the given exit code."""

    done = subprocess.CompletedProcess([], returncode)
    return mock.patch.object(regression_docker.subprocess, "run", return_value=done)


def run_main(argv=[]):
    out = io.StringIO()
    with redirect_stdout(out):
        code = regression_docker.main(argv)
    return code, out.getvalue()


class ExplicitRegressionHarnessTest(unittest.TestCase):
    def test_missing_docker_cli_fails_instead_of_passing(self):
        with docker_status(None), inspect(0):
            code, out = run_main()
        report = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["cases"], [])
        self.assertIn("docker is not on PATH", report["error"])

    def test_unavailable_docker_daemon_fails_instead_of_passing(self):
        with docker_status(1), inspect(0):
            code, out = run_main()
        report = json.loads(out)
        self.assertEqual(code, 1)
        self.assertIn("daemon is not available", report["error"])

    def test_missing_worker_image_fails_with_how_to_fix_it(self):
        with docker_status(0), inspect(1):
            code, out = run_main()
        report = json.loads(out)
        self.assertEqual(code, 1)
        self.assertIn("docker build -t factory-worker:latest worker/", report["error"])


class CaseHonestyTest(unittest.TestCase):
    """A case's status comes from what it recorded, and a pass needs a record."""

    def test_a_case_that_recorded_nothing_does_not_count_as_passed(self):
        case = regression_docker.Case("empty")
        self.assertTrue(case.failed)

    def test_a_failed_record_fails_the_case(self):
        case = regression_docker.Case("half")
        case.observe("command", True)
        case.observe("command", False, "detail")
        self.assertTrue(case.failed)
        self.assertEqual(case.records[1]["detail"], "detail")

    def test_a_case_with_all_passing_records_is_passed(self):
        case = regression_docker.Case("whole")
        case.observe("command", True)
        self.assertFalse(case.failed)

    def test_an_unexpected_exception_marks_its_case_failed(self):
        def blow(image, tmp, case):
            raise runner.WorkerError("case boom")
        with mock.patch.object(regression_docker, "CASES", [("blowup", blow)]), \
                docker_status(0), inspect(0):
            code, out = run_main()
        report = json.loads(out)
        self.assertEqual(code, 1)
        self.assertEqual(report["status"], "failed")
        entry, = report["cases"]
        self.assertEqual(entry["status"], "failed")
        self.assertIn("case boom", entry["records"][0]["detail"])


if __name__ == "__main__":
    unittest.main()
