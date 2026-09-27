"""Tests for templates/repo_lint.py: the drift guard, run from temp directories."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "templates" / "repo_lint.py"


def write(root, rel, text):
    path = os.path.join(root, rel)
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def run_lint(root, *args):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=root, capture_output=True, text=True,
    )


def git(root, *args):
    subprocess.run(
        ["git", *args], cwd=root, capture_output=True, check=True
    )


class RepoLintTest(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="repo-lint-")

    def test_a_missing_path_is_reported_with_its_line(self):
        write(self.root, "AGENTS.md",
              "# AGENTS.md\n\nSee `docs/nope.md` for more.\n")
        proc = run_lint(self.root)
        self.assertEqual(proc.returncode, 1)
        self.assertIn(
            "AGENTS.md:3: path `docs/nope.md` does not exist", proc.stdout
        )

    def test_a_broken_link_is_reported_with_its_line(self):
        write(self.root, "README.md", "# README\n\n[guide](missing.md)\n")
        proc = run_lint(self.root)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("README.md:3: broken link: missing.md", proc.stdout)

    def test_a_link_in_docs_resolves_relative_to_its_own_file(self):
        write(self.root, "docs/a.md", "[b](b.md)\n[gone](gone.md)\n")
        write(self.root, "docs/b.md", "# b\n")
        proc = run_lint(self.root)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("docs/a.md:2: broken link: gone.md", proc.stdout)
        self.assertNotIn("docs/a.md:1:", proc.stdout)

    def test_a_long_file_is_reported_with_line_one(self):
        write(self.root, "app.py", "".join("x = %d\n" % i for i in range(801)))
        proc = run_lint(self.root)
        self.assertEqual(proc.returncode, 1)
        self.assertIn(
            "app.py:1: file has 801 lines, over the 800-line limit",
            proc.stdout,
        )

    def test_a_clean_repo_exits_zero_with_no_output(self):
        write(self.root, "AGENTS.md", "# AGENTS.md\n\nRead `docs/how.md`.\n")
        write(self.root, "README.md",
              "# Project\n\nSee [docs/how.md](docs/how.md).\n")
        write(self.root, "docs/how.md", "# How\n")
        write(self.root, "src/app.py", "def f():\n    return 1\n")
        proc = run_lint(self.root)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")
        self.assertEqual(proc.stderr, "")

    def test_a_bare_filename_that_exists_in_a_subdirectory_is_accepted(self):
        write(self.root, "AGENTS.md", "Start with `runner.py`.\n")
        write(self.root, "worker/runner.py", "pass\n")
        proc = run_lint(self.root)
        self.assertEqual(proc.returncode, 0)

    def test_node_modules_and_lock_files_are_skipped(self):
        write(self.root, "AGENTS.md", "# AGENTS.md\n")
        write(self.root, "node_modules/lib/big.py",
              "".join("x = 1\n" for _ in range(900)))
        write(self.root, "yarn.lock", "".join("a 1\n" for _ in range(900)))
        proc = run_lint(self.root)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_a_git_ignored_file_is_skipped(self):
        git(self.root, "init", "-q")
        write(self.root, ".gitignore", "big.py\n")
        write(self.root, "big.py", "".join("x = 1\n" for _ in range(900)))
        proc = run_lint(self.root)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout, "")

    def test_an_untracked_file_git_does_not_ignore_is_checked(self):
        git(self.root, "init", "-q")
        write(self.root, "big.py", "".join("x = 1\n" for _ in range(900)))
        proc = run_lint(self.root)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("big.py:1:", proc.stdout)

    def test_the_limit_flag_overrides_the_default(self):
        write(self.root, "app.py", "".join("x = %d\n" % i for i in range(21)))
        proc = run_lint(self.root, "--limit", "20")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("app.py:1: file has 21 lines, over the 20-line limit",
                      proc.stdout)


if __name__ == "__main__":
    unittest.main()
