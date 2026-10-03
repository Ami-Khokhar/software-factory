"""bench/: each task's hidden tests pass on its reference solution, so a failure in a run is the
worker's, and each issue names the module its hidden tests import."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

BENCH = Path(__file__).resolve().parent.parent / "bench"
TASKS = sorted(issue.parent.name for issue in BENCH.glob("*/issue.md"))


class BenchTasksTest(unittest.TestCase):
    def test_each_tasks_hidden_tests_pass_on_its_reference_solution(self):
        for task in TASKS:
            with self.subTest(task=task), tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / "bench" / "r0").mkdir(parents=True)
                (Path(tmp) / "tests").mkdir()
                shutil.copy(BENCH / task / "reference.py", Path(tmp) / "bench" / "r0" / f"{task}.py")
                shutil.copy(BENCH / task / "test_hidden.py", Path(tmp) / "tests" / "test_bench_hidden.py")
                done = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests",
                                       "-p", "test_bench_hidden.py"], cwd=tmp, capture_output=True, text=True,
                                      env=dict(os.environ, BENCH_MODULE=f"bench.r0.{task}"))
                self.assertEqual(done.returncode, 0, done.stderr[-3000:])

    def test_each_issue_names_the_module_its_hidden_tests_import_and_has_acceptance(self):
        self.assertTrue(TASKS)
        for task in TASKS:
            with self.subTest(task=task):
                text = (BENCH / task / "issue.md").read_text()
                self.assertIn(f"`{{dir}}/{task}.py`, importable as `bench.{{run}}.{task}`", " ".join(text.split()))
                self.assertRegex(text, r"\n## Acceptance\n(- .+ — done when: .+\n)+$")


if __name__ == "__main__":
    unittest.main()
