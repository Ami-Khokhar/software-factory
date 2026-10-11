"""worker/runner.py: the trusted half of a worker. Real git, a scripted harness, no Docker."""
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from factory_mcp import documents, diagnostics
from factory_mcp.config import Config
from tests.fakes import workitem

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "worker"))
import runner  # noqa: E402

HARNESS = r'''
import os, stat, subprocess, sys, time
mode = sys.argv[1]
def git(*args):
    subprocess.run(["git", *args], check=True, capture_output=True)
if mode in ("fix", "nocommit", "plant"):
    open("app.txt", "w").write("fixed\n")
if mode == "nocommit-broken":
    open("app.txt", "w").write("still broken\n")
if mode == "cheat":
    open(".github/pr-gatekeeper.json", "w").write('{"verify": "true"}')
    open("app.txt", "w").write("still broken\n")
if mode in ("fix", "cheat"):
    git("add", "-A")
    git("-c", "user.name=h", "-c", "user.email=h@h", "commit", "-qm", "harness work")
if mode == "plant":  # a filter driver and hooks, which run wherever git touches this tree
    trap = '[ -n "$FACTORY_TEST_SANDBOX" ] || touch "$FACTORY_TEST_MARKER"'
    open(".gitattributes", "w").write("* filter=plant\n")
    git("config", "filter.plant.clean", trap + "; cat")
    git("config", "filter.plant.smudge", trap + "; cat")
    for hook in ("pre-commit", "post-commit", "post-checkout", "pre-push"):
        path = os.path.join(".git", "hooks", hook)
        open(path, "w").write("#!/bin/sh\n" + trap + "\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)
if mode == "fail":
    print("pi could not reach the model", file=sys.stderr)
    sys.exit(17)
if mode == "timeout":
    print("step one", flush=True)
    print("CODEX_KEY: " + os.environ.get("CODEX_KEY", ""), flush=True)
    # Debugging: every file in every directory the agent logged in with, printed whole.
    for directory in os.environ.get("CREDENTIAL_DIRS", "").split(":"):
        if os.path.isdir(directory):
            for name in sorted(os.listdir(directory)):
                print(open(os.path.join(directory, name)).read(), flush=True)
    print(sys.argv[-1], flush=True)
    time.sleep(30)
print("DONE")
'''
# What the image's system git config and the container boundary give every sandboxed command.
SANDBOX_ENV = {"FACTORY_TEST_SANDBOX": "1", "GIT_AUTHOR_NAME": "factory-worker",
               "GIT_AUTHOR_EMAIL": "w@w", "GIT_COMMITTER_NAME": "factory-worker", "GIT_COMMITTER_EMAIL": "w@w"}


def sh(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


class RunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.origin = root / "origin.git"
        seed, self.checkout = root / "seed", root / "checkout"
        sh(root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        sh(root, "init", "-q", "-b", "main", str(seed))
        (seed / ".github").mkdir()
        (seed / ".github" / "pr-gatekeeper.json").write_text('{"verify": "python3 check.py"}')
        (seed / "check.py").write_text(
            "import sys\nsys.exit(0 if open('app.txt').read().strip() == 'fixed' else 1)\n")
        (seed / "app.txt").write_text("broken\n")
        sh(seed, "add", "-A")
        sh(seed, "-c", "user.name=s", "-c", "user.email=s@s", "commit", "-qm", "seed")
        sh(seed, "push", "-q", str(self.origin), "main")
        self.base = sh(seed, "rev-parse", "HEAD")
        sh(root, "clone", "-q", str(self.origin), str(self.checkout))
        sh(self.checkout, "config", "remote.origin.url", "https://github.com/o/r.git")
        sh(self.checkout, "config", f"url.{self.origin}.insteadOf", "https://github.com/o/r.git")
        (root / "harness.py").write_text(HARNESS)
        self.harness = root / "harness.py"
        self.marker = root / "ran-on-the-host"
        self.auth = root / "auth.json"
        self.auth.write_text('{"opencode-go": {"type": "api", "key": "k"}}')
        self.flags = ["--credential", f"{self.auth}:/home/node/.pi/agent"]
        self.mounts = []
        self.harness_runs = []
        self.sandbox_runs = []

        def local_run(sandbox, argv, seconds, mount, harness=False, env=None):
            """Sandbox.run without Docker: the same argv in the mounted directory, marked sandboxed."""
            self.mounts.append(mount)
            if harness:
                self.sandbox_runs.append(argv)
            if harness:
                self.harness_runs.append({
                    "argv": argv,
                    "seconds": seconds,
                    "env": dict(sandbox.harness_env, **(env or {})),
                    "credentials": [{"target": target, "files": {
                        name: ((Path(scratch) / name).read_text(), (Path(scratch) / name).stat().st_mode & 0o777)
                        for name in os.listdir(scratch)}, "mode": Path(scratch).stat().st_mode & 0o777}
                        for scratch, target in sandbox.credentials],
                })
            self.sandbox_runs.append(argv)
            done = subprocess.run(argv, cwd=mount, capture_output=True, text=True, timeout=60,
                                  env=dict(os.environ, **SANDBOX_ENV, **(env or {})))
            return done.returncode, done.stdout, done.stderr

        # A fake docker CLI: the preflight's `docker image inspect` runs the host command, and the
        # image's own containers are behind the faked Sandbox.run.
        bin = root / "bin"
        bin.mkdir()
        (bin / "docker").write_text(
            '#!/bin/sh\n[ "$1" = image ] && [ -n "$FACTORY_TEST_DOCKER_NO_IMAGE" ] && exit 1\nexit 0\n')
        os.chmod(bin / "docker", 0o755)
        patches = [mock.patch.object(runner.Sandbox, "run", local_run),
                   mock.patch.object(runner, "ensure_docker", lambda: None),
                   mock.patch.dict(os.environ, {"FACTORY_TEST_MARKER": str(self.marker), "PATH":
                                                str(bin) + os.pathsep + os.environ["PATH"]})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def run_order(self, mode, kind="implement", repo="o/r", head=None, remote_verify=False,
                  max_lines=600):
        wi = workitem(id="o/r#7", repo=repo, issueNumber=7, goal="Fix the app", headSha=head)
        config = Config(mcp_token="t", repos=frozenset(),
                        remote_verify_repos=frozenset({repo}) if remote_verify else frozenset())
        order = documents.workorder(wi, kind, 0, config, self.base)
        order["constraints"]["maxChangedLines"] = max_lines
        text = documents.worker_input(order, "factory/7")
        out = io.StringIO()
        cwd = os.getcwd()
        os.chdir(self.checkout)
        try:
            with redirect_stdout(out):
                runner.main([*self.flags, "--", sys.executable, str(self.harness), mode, text])
        finally:
            os.chdir(cwd)
        return json.loads(out.getvalue().strip().splitlines()[-1]), order

    def apply_on(self, commit, diff):
        fresh = Path(self.tmp.name) / f"fresh-{commit[:7]}"
        sh(self.tmp.name, "clone", "-q", str(self.origin), str(fresh))
        sh(fresh, "checkout", "-q", commit)
        subprocess.run(["git", "apply", "--index", "-"], cwd=fresh, input=diff, text=True, check=True)
        return fresh

    def test_a_runner_waits_for_the_checkout_while_another_pass_holds_it(self):
        holder = subprocess.Popen(  # another pass of this repo, fetching for 2.5 seconds
            [sys.executable, "-c", "import fcntl, sys, time\n"
             "f = open(sys.argv[1], 'w'); fcntl.flock(f, fcntl.LOCK_EX); print('held', flush=True); time.sleep(2.5)",
             str(self.checkout / ".git" / "factory-runner.lock")], stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.stdout.close)
        self.assertEqual(holder.stdout.readline().strip(), "held")
        result, _ = self.run_order("fix")
        self.assertEqual(result["status"], "completed", result.get("error"))
        # The wait came out of the harness's share of the pass, which is 798 seconds when nothing waits.
        self.assertLess(self.harness_runs[0]["seconds"], 798)

    def test_completed_patch_applies_on_the_base(self):
        result, order = self.run_order("fix")
        self.assertEqual((result["status"], result["workOrderId"]), ("completed", order["id"]), result.get("error"))
        self.assertEqual((result["patch"]["format"], result["patch"]["baseSha"]), ("unified", self.base))
        self.assertEqual(result["evidence"][0]["command"], "python3 check.py")
        self.assertEqual(result["evidence"][0]["exit"], 0)
        self.assertEqual(documents.check_result(result, order), [])
        self.assertEqual((self.apply_on(self.base, result["patch"]["diff"]) / "app.txt").read_text(), "fixed\n")

    def test_a_remote_verify_order_skips_the_container_verify_and_hands_back_the_patch(self):
        """check.py exits 1 on the app the harness leaves: the verify must not run here (ADR-0045)."""
        result, order = self.run_order("nocommit-broken", remote_verify=True)
        self.assertEqual((result["status"], result["evidence"]), ("completed", []), result.get("error"))
        self.assertEqual(documents.check_result(result, order), [])
        result, _ = self.run_order("nocommit-broken")
        self.assertEqual((result["status"], result["evidence"][0]["exit"]), ("failed", 1))

    def test_the_harness_gets_a_private_copy_of_each_credential_its_env_and_its_budget(self):
        key = Path(self.tmp.name) / "codex.json"
        key.write_text('{"OPENAI_API_KEY": "sk"}')
        self.flags = ["--credential", f"{self.auth}:/home/node/.pi/agent",
                      "--credential", f"{key}:/tmp/factory-home/.codex", "--env", "CODEX_HOME=/tmp/factory-home/.codex"]
        result, _ = self.run_order("fix")
        self.assertEqual(result["status"], "completed", result.get("error"))
        self.assertEqual(len(self.harness_runs), 1)
        run = self.harness_runs[0]
        self.assertEqual(run["argv"][:-1], [sys.executable, str(self.harness), "fix"])
        self.assertEqual(run["argv"][-1].splitlines()[0], "You are a factory worker. Carry out the WorkOrder below in this checkout of o/r.")
        self.assertEqual((run["seconds"], run["env"]), (798, {"CODEX_HOME": "/tmp/factory-home/.codex"}))
        self.assertEqual(run["credentials"], [
            {"target": "/home/node/.pi/agent", "mode": 0o700,
             "files": {"auth.json": ('{"opencode-go": {"type": "api", "key": "k"}}', 0o600)}},
            {"target": "/tmp/factory-home/.codex", "mode": 0o700,
             "files": {"codex.json": ('{"OPENAI_API_KEY": "sk"}', 0o600)}}])

    def test_a_missing_credential_fails_the_pass_with_its_path_before_any_container(self):
        missing = Path(self.tmp.name) / "nowhere.json"
        self.flags = ["--credential", f"{missing}:/home/node/.pi/agent"]
        result, _ = self.run_order("fix")
        self.assertEqual((result["status"], result["error"]), ("failed", f"no model credential at {missing}"))
        self.assertEqual(self.mounts, [])

    def test_uncommitted_work_is_collected(self):
        result, _ = self.run_order("nocommit")
        self.assertEqual(result["status"], "completed", result.get("error"))
        self.assertIn("+fixed", result["patch"]["diff"])

    def test_no_change_fails(self):
        result, order = self.run_order("nothing")
        self.assertEqual((result["status"], result["error"]), ("failed", "the harness made no change"))
        self.assertEqual(documents.check_result(result, order), [])

    def test_a_whole_pull_request_over_its_limits_fails_before_the_verify(self):
        """A fix on an open PR is measured with the implementation it follows: the branch's whole
        diff against the default branch's merge base must fit the WorkOrder (ADR-0040), and the
        runner refuses it before the verify runs, with the actual size and what remains."""
        branch = Path(self.tmp.name) / "branch"
        sh(self.tmp.name, "clone", "-q", str(self.origin), str(branch))
        sh(branch, "checkout", "-q", "-b", "factory/7")
        (branch / "notes.txt").write_text("note\n" * 601)  # the prior implementation: 601 lines
        sh(branch, "add", "-A")
        sh(branch, "-c", "user.name=w", "-c", "user.email=w@w", "commit", "-qm", "round 1")
        sh(branch, "push", "-q", "origin", "factory/7")
        head = sh(branch, "rev-parse", "HEAD")
        result, _ = self.run_order("fix", kind="fix", head=head)
        self.assertEqual(result["status"], "failed", result)
        self.assertIn("603 lines in 2 files", result["error"])
        self.assertIn("leaves -3 lines", result["error"])
        phase = self.phases_of(result)["patch collection"]
        self.assertIn("603 lines in 2 files", phase.get("tail", ""))
        self.assertNotIn("verification", self.phases_of(result))
        self.assertEqual(result["evidence"], [])
        verify_runs = [argv for argv in self.sandbox_runs
                       if argv[:2] == ["sh", "-c"] and "check.py" in argv[2]]
        self.assertEqual(verify_runs, [], "the verify ran on an oversized pull request")

    def test_a_whole_pull_request_at_its_limit_is_collected(self):
        result, order = self.run_order("fix", max_lines=2)  # app.txt: one in, one out
        self.assertEqual(result["status"], "completed", result.get("error"))
        self.assertEqual(documents.check_result(result, order), [])

    def test_the_worker_gets_the_whole_pr_size_check(self):
        result, _ = self.run_order("fix")
        self.assertEqual(result["status"], "completed", result.get("error"))
        text = self.harness_runs[0]["argv"][-1]
        self.assertIn("git diff --shortstat $(git merge-base origin/HEAD HEAD) HEAD", text)
        self.assertIn("including the commits already on the branch", text)

    def test_a_failed_harness_fails_the_pass_before_collecting(self):
        result, _ = self.run_order("fail")
        self.assertEqual((result["status"], result["error"]),
                         ("failed", "the harness exited 17: pi could not reach the model"))

    def test_a_checkout_of_another_repo_is_refused(self):
        result, _ = self.run_order("fix", repo="o/other")
        self.assertEqual(result["status"], "failed")
        self.assertIn("is o/r, not o/other", result["error"])

    def test_verify_comes_from_the_base_commit(self):
        result, _ = self.run_order("cheat")  # rewrote verify to `true` and left the app broken
        self.assertEqual(result["status"], "failed")
        self.assertEqual((result["evidence"][0]["command"], result["evidence"][0]["exit"]), ("python3 check.py", 1))

    def test_fix_builds_on_the_pr_head(self):
        branch = Path(self.tmp.name) / "branch"
        sh(self.tmp.name, "clone", "-q", str(self.origin), str(branch))
        (branch / "notes.txt").write_text("round 1\n")
        sh(branch, "checkout", "-q", "-b", "factory/7")
        sh(branch, "add", "-A")
        sh(branch, "-c", "user.name=w", "-c", "user.email=w@w", "commit", "-qm", "round 1")
        sh(branch, "push", "-q", "origin", "factory/7")
        head = sh(branch, "rev-parse", "HEAD")
        result, _ = self.run_order("fix", kind="fix", head=head)
        self.assertEqual((result["status"], result["patch"]["baseSha"]), ("completed", head), result.get("error"))
        self.assertNotIn("notes.txt", result["patch"]["diff"])
        fresh = self.apply_on(head, result["patch"]["diff"])
        self.assertEqual(((fresh / "notes.txt").exists(), (fresh / "app.txt").read_text()), (True, "fixed\n"))

    def test_a_fetch_in_the_checkout_finishes_its_repack_before_it_returns(self):
        """prepare clones the checkout right after it fetches there, so a repack still running in
        the background could delete a pack the clone copies (#94)."""
        # One pack, and a fetch that brings a second one, so the fetch starts an auto repack.
        for key, value in (("gc.autoPackLimit", "1"), ("transfer.unpackLimit", "1")):
            sh(self.checkout, "config", key, value)
        sh(self.checkout, "repack", "-q", "-d")
        seed = Path(self.tmp.name) / "seed"
        (seed / "more.txt").write_text("more\n")
        sh(seed, "add", "-A")
        sh(seed, "-c", "user.name=s", "-c", "user.email=s@s", "commit", "-qm", "more")
        sh(seed, "push", "-q", str(self.origin), "main")
        runner.git(str(self.checkout), "fetch", "-q", "--prune", "origin")
        packs = list((self.checkout / ".git" / "objects" / "pack").glob("*.pack"))
        self.assertEqual(len(packs), 1, "the repack was still running when the fetch returned")

    def test_git_settings_the_environment_already_has_are_kept_beside_foreground_maintenance(self):
        env = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "user.factorytest", "GIT_CONFIG_VALUE_0": "kept"}
        with mock.patch.dict(os.environ, env):
            self.assertEqual(runner.git(str(self.checkout), "config", "--get", "user.factorytest"), "kept")
            self.assertEqual(runner.git(str(self.checkout), "config", "--get", "maintenance.autoDetach"), "false")

    def test_a_git_command_that_times_out_is_named(self):
        with mock.patch.object(runner.subprocess, "run", side_effect=subprocess.TimeoutExpired(["git"], 900)), \
                self.assertRaisesRegex(runner.WorkerError, r"^git fetch timed out after 900s$"):
            runner.git(str(self.checkout), "fetch", "-q", "origin", timeout=900)

    def test_a_malformed_credential_or_env_fails_the_pass_before_any_container(self):
        for flags, error in ((["--credential", "/tmp/nowhere"], "--credential '/tmp/nowhere' is not HOST_FILE:/container/dir"),
                             (["--env", "NOEQUALS"], "--env 'NOEQUALS' is not KEY=VALUE")):
            with self.subTest(error):
                self.flags, self.mounts = flags, []
                result, _ = self.run_order("fix")
                self.assertEqual((result["status"], result["error"], self.mounts), ("failed", error, []))

    def test_the_harness_gets_a_copy_and_never_the_checkout(self):
        before = (sh(self.checkout, "rev-parse", "HEAD"), (self.checkout / ".git" / "config").read_text())
        result, _ = self.run_order("plant")
        self.assertEqual(result["status"], "completed", result.get("error"))
        self.assertTrue(self.mounts)
        for mount in self.mounts:
            self.assertFalse(Path(mount).resolve().is_relative_to(self.checkout.resolve()), mount)
            self.assertFalse(Path(mount).exists(), "the copy is removed after the pass")
        self.assertEqual((sh(self.checkout, "rev-parse", "HEAD"), (self.checkout / ".git" / "config").read_text()),
                         before)
        self.assertEqual(sh(self.checkout, "status", "--porcelain"), "")

    def phases_of(self, result):
        return {phase["phase"]: phase for phase in result.get("diagnostics", {}).get("phases", [])}

    def test_diagnostics_cover_every_phase_of_a_pass(self):
        result, order = self.run_order("fix")
        self.assertEqual(result["status"], "completed", result.get("error"))
        phases = self.phases_of(result)
        self.assertEqual(set(phases), {"checkout", "environment setup", "harness execution",
                                       "patch collection", "verification"})
        for name, phase in phases.items():
            self.assertTrue(0 <= phase["elapsedMs"] < 60_000, (name, phase))
        self.assertEqual(phases["harness execution"]["exitCode"], 0)
        self.assertEqual(phases["patch collection"]["exitCode"], 0)
        self.assertEqual(phases["verification"]["exitCode"], 0)
        self.assertTrue(0 <= result["diagnostics"]["elapsedMs"] < 120_000)
        self.assertEqual(documents.check_result(result, order), [], result["diagnostics"])

    def test_a_failing_harness_or_verify_names_its_phase_and_exit(self):
        result, _ = self.run_order("fail")
        phase = self.phases_of(result)["harness execution"]
        self.assertEqual((result["status"], phase["exitCode"]), ("failed", 17))
        self.assertIn("pi could not reach the model", phase["tail"])
        result, _ = self.run_order("cheat")
        phase = self.phases_of(result)["verification"]
        self.assertEqual((result["status"], phase["exitCode"]), ("failed", 1))
        self.assertIn("python3 check.py", result["evidence"][0]["command"])

    def test_a_failing_patch_collection_names_its_phase_and_exit(self):
        result, _ = self.run_order("nothing")
        self.assertEqual((result["status"], result["error"]),
                         ("failed", "the harness made no change"))
        phase = self.phases_of(result)["patch collection"]
        self.assertEqual(("exitCode" in phase, phase.get("exitCode") is None), (False, True))
        original = runner.Sandbox.run

        def collected(sandbox, argv, seconds, mount, harness=False, env=None):
            """Collecting with git: the sandbox's own exit code rides. The preflight's own probe
            (a `sh -c` command -v) still runs as it would, so only collection is faked."""
            if harness:
                return original(sandbox, argv, seconds, mount, harness=True, env=env)
            if argv[:2] == ["sh", "-c"] and argv[2].startswith("command -v"):
                return original(sandbox, argv, seconds, mount, harness=False, env=env)
            return 3, "", "not a commit we know"

        with mock.patch.object(runner.Sandbox, "run", collected):
            result, _ = self.run_order("fix")
        self.assertEqual((result["status"], self.phases_of(result)["patch collection"]["exitCode"]),
                         ("failed", 3))

    def test_preflight_stops_the_harness_when_the_environment_is_unavailable(self):
        """#193: every part of the environment is checked before the coding harness runs. A check
        that fails ends the pass with no harness invocation, names itself with a nonzero exit code
        and a bounded tail that carries no host path, and reads to the pause consumer as the
        environment (ADR-0051), not as the harness's work."""
        def missing_harness(sandbox, argv, seconds, mount, harness=False, env=None):
            if argv[:2] == ["sh", "-c"] and argv[2].startswith("command -v"):
                return 127, "", ""
            return original_run(sandbox, argv, seconds, mount, harness=harness, env=env)

        original_run = runner.Sandbox.run
        cases = [
            ("daemon",
             [mock.patch.object(runner, "ensure_docker", side_effect=runner.WorkerError(
                 "docker is not available: OrbStack did not start", runner.NO_DOCKER))],
             runner.NO_DOCKER, runner.PREFLIGHT_TAILS[runner.NO_DOCKER]),
            ("image", [mock.patch.dict(os.environ, {"FACTORY_TEST_DOCKER_NO_IMAGE": "1"})],
             runner.NO_IMAGE, runner.PREFLIGHT_TAILS[runner.NO_IMAGE]),
            ("credential",
             [mock.patch.object(shutil, "copyfile",
                                side_effect=PermissionError(13, "Permission denied"))],
             runner.NO_CREDENTIAL, runner.PREFLIGHT_TAILS[runner.NO_CREDENTIAL]),
            ("verify", [mock.patch.object(runner, "verify_command", lambda *a, **k: None)],
             runner.NO_VERIFY, runner.PREFLIGHT_TAILS[runner.NO_VERIFY]),
            ("harness", [mock.patch.object(runner.Sandbox, "run", missing_harness)],
             runner.NO_HARNESS, runner.PREFLIGHT_TAILS[runner.NO_HARNESS]),
        ]
        for name, patches, code, tail in cases:
            with self.subTest(name):
                for patch in patches:
                    patch.start()
                    try:
                        self.one_preflight_failure(code, tail)
                    finally:
                        patch.stop()

    def one_preflight_failure(self, code, tail):
        """One preflight failure: no harness invocation, the failing check's own code and a bounded
        tail; the pause consumer reads the pass as the environment, not the harness's work."""
        result, order = self.run_order("fix")
        self.assertEqual(result["status"], "failed", result)
        self.assertEqual(self.harness_runs, [], "the harness ran on a broken environment")
        phase = self.phases_of(result)["environment setup"]
        self.assertEqual(phase.get("exitCode"), code, result.get("error"))
        self.assertEqual(phase.get("tail", ""), tail)
        self.assertEqual(len(tail), len(tail[:runner.DIAGNOSTIC_TAIL]), "tail is not bounded")
        self.assertNotIn(str(self.tmp.name), result["diagnostics"]["phases"])
        self.assertEqual(diagnostics.diagnose(order, result)["cause"], "checkout or setup")
        self.assertTrue(diagnostics.environment_fault(diagnostics.diagnose(order, result)["cause"]))

    def test_preflight_leaves_healthy_and_code_failed_passes_alone(self):
        """A working environment runs the harness exactly as before, and a harness that fails on
        its own code is not the environment: the preflight phase exits 0 and the diagnosis of the
        code failure stays with the harness phase."""
        result, order = self.run_order("fix")
        self.assertEqual(result["status"], "completed", result.get("error"))
        self.assertEqual(len(self.harness_runs), 1)
        self.assertEqual(self.phases_of(result)["environment setup"].get("exitCode"), 0)
        result, _ = self.run_order("fail")
        self.assertEqual(self.phases_of(result)["environment setup"].get("exitCode"), 0)
        self.assertEqual(diagnostics.diagnose(order, result)["cause"], None)

    def test_a_failing_checkout_or_setup_names_its_phase_and_exit(self):
        """A phase the consumer can name is one that carries the command's exit code when there is
        one (ADR-0051): a broken checkout still pauses with the repair reason, not a second pass."""
        real_git = runner.git

        def broken_fetch(cwd, *args, **kwargs):
            if args[0] == "fetch":
                raise runner.WorkerError("git fetch failed: fatal: dead remote", 128)
            return real_git(cwd, *args, **kwargs)

        with mock.patch.object(runner, "git", broken_fetch):
            result, _ = self.run_order("fix")
        self.assertEqual((result["status"], result["error"]),
                         ("failed", "git fetch failed: fatal: dead remote"))
        self.assertEqual(self.phases_of(result)["checkout"].get("exitCode"), 128)
        with mock.patch.object(runner, "prepare", side_effect=runner.WorkerError("git fetch failed")):
            result, _ = self.run_order("fix")
        self.assertEqual((result["status"], result["error"]), ("failed", "git fetch failed"))
        self.assertIn("checkout", self.phases_of(result))
        with mock.patch.object(runner, "ensure_docker", side_effect=runner.WorkerError(
                "docker is not available: OrbStack did not start", 7)):
            result, _ = self.run_order("fix")
        self.assertEqual((result["status"],
                          self.phases_of(result)["environment setup"].get("exitCode")), ("failed", 7))

    def test_a_harness_timeout_keeps_its_output_and_redacts_secrets(self):
        """The container's kill returns what it printed before it died (#192): the diagnostics tail
        holds it with the copied credential's content, the env value and the WorkOrder's command
        payload removed, along with the phase's timing"""
        # The multi-line credential is the ordinary case for an INI or pretty-printed auth file:
        # its content matches the whitespace-collapsed tail only through the collapsed secret the
        # runner registers, and neither value is credential-shaped enough for the other rules.
        key = Path(self.tmp.name) / "codex.json"
        key.write_text("hunter-file9 = opendoor\nsecond = line\n")
        self.auth.write_text("hunter-agent=opendoor")
        self.flags = ["--credential", f"{self.auth}:/home/node/.pi/agent",
                      "--credential", f"{key}:/tmp/factory-home/.codex",
                      "--env", "CODEX_KEY=sk-hunter2"]
        sandbox_timeout = []

        def timed_out_run(sandbox, argv, seconds, mount, harness=False, env=None):
            """Sandbox.run without Docker: the kill is `timeout`, like the image's command, so the
            exit is 124 with everything the harness printed before it."""
            if harness:
                sandbox_timeout.append(seconds)
                env = dict(sandbox.harness_env, **(env or {}))
                env.pop("CREDENTIAL_DIRS", None)
                env["CREDENTIAL_DIRS"] = ":".join(scratch for scratch, _ in sandbox.credentials)
                argv = ["timeout", "--kill-after=1", "1", *argv]
            try:
                done = subprocess.run(argv, cwd=mount, capture_output=True, text=True,
                                      timeout=60,
                                      env=dict(os.environ, **SANDBOX_ENV, **(env or {})))
            except subprocess.TimeoutExpired as exc:
                return 124, exc.stdout or "", exc.stderr or ""
            return done.returncode, done.stdout, done.stderr

        with mock.patch.object(runner.Sandbox, "run", timed_out_run):
            result, order = self.run_order("timeout")
        self.assertTrue(sandbox_timeout, "the harness ran with a sandbox timeout at all")
        self.assertEqual((result["status"], result["error"]), ("failed", "the harness timed out"))
        phase = self.phases_of(result)["harness execution"]
        self.assertEqual((phase["exitCode"], 0 < phase["elapsedMs"]), (124, True))
        self.assertEqual(documents.check_result(result, order), [], result["diagnostics"])
        tail = phase["tail"]
        self.assertIn("step one", tail)
        self.assertGreaterEqual(phase["elapsedMs"], 900)
        # The configured env value: the harness prints CODEX_KEY's value itself.
        self.assertNotIn("sk-hunter2", tail)
        self.assertNotIn("hunter2", tail)
        # The copied credential's content: the harness prints the file itself, so only the
        # runner's credential-reading loop can keep it out of the tail.
        self.assertNotIn("hunter-file9", tail)
        self.assertNotIn("hunter-agent", tail)

        self.assertNotIn("opendoor", tail)
        self.assertNotIn("factory.workorder/v1", tail)
        self.assertNotIn("Fix the app", tail)

    def test_hooks_and_filters_the_harness_plants_never_run_on_the_host(self):
        result, _ = self.run_order("plant")
        self.assertEqual(result["status"], "completed", result.get("error"))
        self.assertIn("+fixed", result["patch"]["diff"])
        self.assertFalse(self.marker.exists(), "host git ran in a tree the harness wrote")


class SandboxTest(unittest.TestCase):
    def test_every_sandbox_command_uses_the_host_user_and_writable_home(self):
        with mock.patch.object(runner.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout="", stderr="")) as run:
            sandbox = runner.Sandbox("image", "label", [("/private/credential-0", "/home/node/.pi/agent")],
                                     {"PI_CODING_AGENT_DIR": "/home/node/.pi/agent"})
            sandbox.run(["pi"], 10, "/private/work", harness=True)
            sandbox.run(["sh", "-c", "git diff"], 10, "/private/work")
        harness, collect = (call.args[0] for call in run.call_args_list)
        for command in (harness, collect):
            self.assertIn("--user", command)
            self.assertIn(f"{os.getuid()}:{os.getgid()}", command)
            self.assertIn(f"HOME={runner.CONTAINER_HOME}", command)
        for item in ("/private/credential-0:/home/node/.pi/agent", "PI_CODING_AGENT_DIR=/home/node/.pi/agent"):
            self.assertIn(item, harness)
            self.assertNotIn(item, collect)

    def test_the_sandbox_returns_the_output_it_captured_when_the_timeout_fires(self):
        """A docker CLI that hangs is killed like the harness it started; whatever it had printed
        by then rides on the TimeoutExpired and becomes the Result's output (#192)."""
        expired = subprocess.TimeoutExpired(["pi"], 10, output=b"partial print\n", stderr=b"late logging")
        with mock.patch.object(runner.subprocess, "run",
                               side_effect=[expired, SimpleNamespace(returncode=0, stdout="", stderr="")]):
            code, out, err = runner.Sandbox("image", "label").run(["pi", "x"], 10, "/works")
        self.assertEqual((code, out, err), (124, "partial print\n", "late logging"))

    def test_a_timeout_with_no_captured_output_still_says_so(self):
        expired = subprocess.TimeoutExpired(["pi"], 10)
        with mock.patch.object(runner.subprocess, "run",
                               side_effect=[expired, SimpleNamespace(returncode=0, stdout="", stderr="")]):
            code, out, err = runner.Sandbox("image", "label").run(["pi", "x"], 10, "/works")
        self.assertEqual((code, out, err), (124, "", "timed out"))

        with mock.patch.object(runner.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout="", stderr="")) as run:
            sandbox = runner.Sandbox("image", "label", [("/private/credential-0", "/home/node/.pi/agent")],
                                     {"PI_CODING_AGENT_DIR": "/home/node/.pi/agent"})
            sandbox.run(["pi"], 10, "/private/work", harness=True)
            sandbox.run(["sh", "-c", "git diff"], 10, "/private/work")
        harness, collect = (call.args[0] for call in run.call_args_list)
        for command in (harness, collect):
            self.assertIn("--user", command)
            self.assertIn(f"{os.getuid()}:{os.getgid()}", command)
            self.assertIn(f"HOME={runner.CONTAINER_HOME}", command)
        for item in ("/private/credential-0:/home/node/.pi/agent", "PI_CODING_AGENT_DIR=/home/node/.pi/agent"):
            self.assertIn(item, harness)
            self.assertNotIn(item, collect)


class EnsureDockerTest(unittest.TestCase):
    """After a reboot Docker is down; the pass starts OrbStack instead of failing for a person."""

    def stubs(self, orb_starts):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        bin_, up = Path(tmp.name), Path(tmp.name) / "up"
        (bin_ / "docker").write_text(f'#!/bin/sh\n[ -e "{up}" ]\n')
        (bin_ / "orb").write_text(f'#!/bin/sh\n{"touch " + repr(str(up)) if orb_starts else "true"}\n')
        (bin_ / "open").write_text("#!/bin/sh\ntrue\n")
        for name in ("docker", "orb", "open"):
            os.chmod(bin_ / name, 0o755)
        return mock.patch.dict(os.environ, {"PATH": f"{bin_}:{os.environ['PATH']}"}), up

    def test_a_stopped_docker_is_started(self):
        env, up = self.stubs(orb_starts=True)
        with env:
            runner.ensure_docker(wait=5, poll=0.05)
        self.assertTrue(up.exists())

    def test_docker_that_does_not_start_fails_the_pass(self):
        env, _ = self.stubs(orb_starts=False)
        with env, self.assertRaisesRegex(runner.WorkerError, "docker is not available") as raised:
            runner.ensure_docker(wait=0.3, poll=0.05)
        # The daemon failure carries the preflight code the pause consumer reads as the #193 environment.
        self.assertEqual(raised.exception.code, runner.NO_DOCKER)


if __name__ == "__main__":
    unittest.main()
