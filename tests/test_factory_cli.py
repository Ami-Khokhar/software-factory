"""scripts/factory: `update` deploys a branch only when its tests pass, and follows it to main once
merged; `add-repo` asks factory-mcp, as Day Desk does; `watch` reports each escalation once;
`report` fits the week in one message; `stop` sends the relay its token; `bench` files the
benchmark's tasks and grades the merged code with their hidden tests."""
import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import factory_mcp.tools  # the factory tools, to compare what the deployed script ships with

LOADER = importlib.machinery.SourceFileLoader(
    "factory_cli", str(Path(__file__).resolve().parent.parent / "scripts" / "factory"))
cli = importlib.util.module_from_spec(importlib.util.spec_from_loader("factory_cli", LOADER))
LOADER.exec_module(cli)
ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
           GIT_COMMITTER_EMAIL="t@t")
AGENT_TOOLS = factory_mcp.tools.AGENT_TOOLS
def probe_output(names):
    return "  ✓ Connected (1ms)\n  ✓ Tools discovered: " + str(len(names)) + "\n\n" + "".join(
        f"    {name:<36} Description\n" for name in names)


PASS = "import unittest\n\n\nclass T(unittest.TestCase):\n    def test(self):\n        self.assertTrue(True)\n"


class McpEndpoint(ThreadingHTTPServer):
    """A controlled factory-mcp endpoint: tools/list answers what a token's listing names.

    The request history is per instance: a test's server makes it fresh and only own requests
    appear on it, so one test's endpoint cannot be tainted by another test's.
    """

    daemon_threads = True

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.asked = ()

    def answer(self, request):
        self.asked = (*self.asked, request.headers.get("Authorization", ""))
        names = self.listings.get(request.headers.get("Authorization", ""), None)
        body = ({"jsonrpc": "2.0", "id": 1, "result": {"tools": [{"name": name} for name in names]}}
                if names is not None else {"error": "unauthorized"})
        data = json.dumps(body).encode()
        request.send_response(200 if names is not None else 401)
        if names is not None:
            request.send_header("content-type", "application/json")
        request.send_header("content-length", str(len(data)))
        request.end_headers()
        request.wfile.write(data)


class McpRequest(BaseHTTPRequestHandler):
    def do_POST(self):
        self.server.answer(self)

    def log_message(self, *args):
        pass


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, env=ENV, check=True, capture_output=True,
                          text=True).stdout.strip()


class NewIssueTest(unittest.TestCase):
    def new(self, *args):
        with mock.patch.object(cli.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)) as run, \
                self.assertRaises(SystemExit):
            cli.cmd_new(["o/r", "Greet by name", "greet says hello | test_greet passes", *args])
        return run.call_args.kwargs["input"]

    def test_each_w_names_a_worker_in_a_worker_line_in_order(self):
        self.assertEqual(self.new("-w", "opencode-glm-factory", "-w", "pi-glm-factory"),
                         "Greet by name\n\nWorker: opencode-glm-factory\nWorker: pi-glm-factory\n\n"
                         "## Acceptance\n- greet says hello — done when: test_greet passes\n")

    def test_without_w_the_issue_has_no_worker_line(self):
        self.assertEqual(self.new(), "Greet by name\n\n## Acceptance\n- greet says hello — done when: test_greet passes\n")


class UpdateTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.origin, self.dev, self.deploy = root / "origin.git", root / "dev", root / "deploy"
        git(root, "init", "-q", "--bare", "-b", "main", str(self.origin))
        git(root, "clone", "-q", str(self.origin), str(self.dev))
        self.commit("tests/test_ok.py", PASS)
        git(self.dev, "push", "-q", "origin", "HEAD:main")
        git(self.dev, "push", "-q", "origin", "HEAD:main")
        # The template config.yaml, where the deploy reads the owner's include list from, is in the
        # repo the tests deploy (#188).
        template = Path(__file__).resolve().parent.parent / "tenant" / "hermes" / "config.yaml"
        self.commit("tenant/hermes/config.yaml", template.read_text())
        git(self.dev, "push", "-q", "origin", "HEAD:main")
        git(root, "clone", "-q", str(self.origin), str(self.deploy))
        # A config dir with no secrets file, so `service_env` builds the deploy environment from
        # the patched SETTINGS alone and never the real ~/.config/factory-mcp (finding 1ff5bc93).
        config = root / "config"
        self.config = config
        self.mcp = McpEndpoint(("127.0.0.1", 0), McpRequest)
        self.mcp.listings = {"Bearer owner-token": self.include(),
                             "Bearer agent-token": sorted(AGENT_TOOLS)}
        threading.Thread(target=self.mcp.serve_forever, daemon=True).start()
        self.addCleanup(self.mcp.shutdown)
        # The host may set credential-bearing variables in its own environment; `service_env`
        # reads `os.environ` first, so strip them (and restore them afterward) to keep what the
        # tests exercise dummy-only: a real or inherited token must never reach the endpoint.
        stripped = {key: os.environ.pop(key) for key in ("FACTORY_MCP_TOKEN", "FACTORY_AGENT_TOKEN")
                    if key in os.environ}
        self.addCleanup(os.environ.update, stripped)
        port = self.mcp.server_address[1]
        self.restarts = []
        for p in (mock.patch.object(cli, "HERE", self.deploy), mock.patch.object(cli, "STATE", root / "state"),
                  mock.patch.object(cli, "CONFIG", config),
                  mock.patch.object(cli, "restart_factory_mcp", lambda: self.restarts.append(1)),
                  mock.patch.object(cli, "branch_merged", lambda branch: False),
                  mock.patch.object(cli, "PORT", port),
                  mock.patch.object(cli, "SETTINGS", {"FACTORY_MCP_TOKEN": "owner-token",
                                                       "FACTORY_AGENT_TOKEN": "agent-token"})):
            p.start()
            self.addCleanup(p.stop)

    def include(self):
        """The owner tool names the real template includes, as the deploy reads them."""
        template = Path(__file__).resolve().parent.parent / "tenant" / "hermes" / "config.yaml"
        return re.findall(r"^\s*-\s*(factory_\w+)\s*$", template.read_text(), re.M)


    def change_script(self, text):
        """Commit a scripts/factory whose deploy_steps runs `text` first, as an update commit would
        that introduces a new deploy step."""
        script = (Path(__file__).resolve().parent.parent / "scripts" / "factory").read_text()
        anchor = "    if restart:  # a retry restarted already, unless it rebuilds the worker\n"
        self.commit("scripts/factory", script.replace(anchor, text + anchor))
        git(self.dev, "push", "-q", "origin", "HEAD:main")

    def test_a_changed_update_script_runs_its_new_deploy_step_once(self):
        self.change_script('    marker = HERE / "deploy-step.log"\n'
                           '    marker.write_text((marker.read_text() if marker.exists() else "") + "step\\n")\n')
        commands, printed = self.deploy_with()
        self.assertEqual((self.deploy / "deploy-step.log").read_text(), "step\n")  # the new step, once
        self.assertIn("update: deployed", printed)
        self.assertEqual(len(self.restarts), 1)  # and factory-mcp still restarts once
        self.assertEqual(self.deploy_with(), ([], ""))  # and nothing runs again on the next update

    def test_a_failing_new_deploy_step_keeps_the_record_and_succeeds_on_the_next_update(self):
        self.change_script('    marker = HERE / "deploy-step.log"\n'
                           '    if not marker.exists():\n'
                           '        marker.write_text("step\\n")\n'
                           '        die("update: the new step failed; the next update tries again")\n')
        with self.assertRaisesRegex(SystemExit,
                                    r"the new update script failed at [0-9a-f]{7}[\s\S]*the new step failed"):
            self.deploy_with()
        record = (cli.STATE / "deployed").read_text()  # the previous successful baseline is kept
        self.assertEqual(len(self.restarts), 0)  # factory-mcp runs the old code until the steps succeed
        commands, printed = self.deploy_with()  # the same commit deploys now, without a guard loop
        self.assertIn("update: deployed", printed)
        self.assertEqual(len(self.restarts), 1)  # and that retry still restarted factory-mcp onto the new code
        self.assertNotEqual((cli.STATE / "deployed").read_text(), record)
        self.assertEqual((self.deploy / "deploy-step.log").read_text(), "step\n")
        self.assertEqual(self.deploy_with(), ([], ""))

    def test_the_reexecuted_script_deploys_from_the_record_its_parent_passes(self):
        # The child once read the ambient state file, which names a commit this checkout has never
        # seen and makes every path look changed (the finding #187 r1:630f6b1a058d856b).
        (cli.STATE / "deployed").parent.mkdir(parents=True, exist_ok=True)
        (cli.STATE / "deployed").write_text("0" * 40 + "\n")
        since = self.commit("notes.txt", "x\n")
        git(self.dev, "push", "-q", "origin", "HEAD:main")
        new = self.commit("worker/Dockerfile", "FROM scratch\n")
        git(self.dev, "push", "-q", "origin", "HEAD:main")
        git(self.deploy, "fetch", "-q", "origin")
        git(self.deploy, "checkout", "-q", new)
        with self.assertRaisesRegex(SystemExit, "not a command to run by hand"):
            cli.cmd_update(["--deploy-steps"])  # no UPDATE_SINCE: nothing to start from
        with mock.patch.dict(os.environ, {cli.UPDATE_SINCE: since}), \
                mock.patch.object(cli, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            cli.cmd_update(["--deploy-steps"])
        self.assertEqual([call.args for call in run.call_args_list], [
            ("docker", "build", "-q", "-t", "factory-worker:candidate", str(self.deploy / "worker")),
            ("docker", "run", "--rm", "factory-worker:candidate", "sh", "-c", cli.WORKER_CHECK),
            ("docker", "tag", "factory-worker:candidate", "factory-worker:latest")])

    def commit(self, name, text, cwd=None, message=None):
        cwd = cwd or self.dev
        (cwd / name).parent.mkdir(parents=True, exist_ok=True)
        (cwd / name).write_text(text)
        git(cwd, "add", "-A")
        git(cwd, "commit", "-q", "-m", message or f"change {name}")
        return git(cwd, "rev-parse", "HEAD")

    def head(self):
        return git(self.deploy, "rev-parse", "HEAD"), git(self.deploy, "branch", "--show-current")

    def test_a_passing_commit_is_deployed_and_factory_mcp_restarted(self):
        new = self.commit("notes.txt", "x\n")
        git(self.dev, "push", "-q", "origin", "HEAD:main")
        cli.cmd_update([])
        self.assertEqual((self.head(), len(self.restarts)), ((new, "main"), 1))
        cli.cmd_update([])  # nothing new: no restart
        self.assertEqual(len(self.restarts), 1)

    def test_a_dockerfile_update_rebuilds_the_worker_image(self):
        self.commit("worker/Dockerfile", "FROM scratch\n")
        git(self.dev, "push", "-q", "origin", "HEAD:main")
        with mock.patch.object(cli, "run", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            cli.cmd_update([])
        self.assertEqual([call.args for call in run.call_args_list], [
            ("docker", "build", "-q", "-t", "factory-worker:candidate", str(self.deploy / "worker")),
            ("docker", "run", "--rm", "factory-worker:candidate", "sh", "-c", cli.WORKER_CHECK),
            ("docker", "tag", "factory-worker:candidate", "factory-worker:latest")])

    def test_a_worker_image_without_python_never_becomes_latest(self):
        self.commit("worker/Dockerfile", "FROM scratch\n")
        git(self.dev, "push", "-q", "origin", "HEAD:main")
        ran = []

        def run(*cmd, **_):
            ran.append(cmd)
            missing = cmd[:3] == ("docker", "run", "--rm")
            return subprocess.CompletedProcess([], 127 if missing else 0, "", "sh: 1: python: not found")
        with mock.patch.object(cli, "run", side_effect=run), \
                self.assertRaisesRegex(SystemExit, "worker image check failed at [0-9a-f]{7}: sh: 1: python: not found"):
            cli.cmd_update([])
        self.assertNotIn(("docker", "tag", "factory-worker:candidate", "factory-worker:latest"), ran)

    def deploy_with(self, name=None, text="", results=None):
        """Deploy one commit that changes `name` (none: deploy again); return the commands run and what
        was printed. `results` maps a command's first three words to its CompletedProcess or exception."""
        if name:
            self.commit(name, text)
            git(self.dev, "push", "-q", "origin", "HEAD:main")
        printed = io.StringIO()
        shown = f"Profile: day-desk\nInstalled from: {self.deploy / 'tenant' / 'hermes'}\n"
        defaults = [("hermes -p day-desk config get mcp_servers.factory_drive.tools --json",
                     subprocess.CompletedProcess([], 1, "", "Config key not set: mcp_servers.factory_drive.tools\n")),
                    ("hermes -p day-desk config get mcp_servers.factory.tools --json",
                     subprocess.CompletedProcess([], 0, json.dumps({"include": self.include()}), "")),
                    ("hermes profile show", subprocess.CompletedProcess([], 0, shown, "")),
                    ("hermes -p day-desk mcp test factory_drive",
                     subprocess.CompletedProcess([], 0, probe_output(sorted(AGENT_TOOLS)), "")),
                    ("hermes -p day-desk mcp test factory",
                     subprocess.CompletedProcess([], 0, probe_output(self.include()), ""))]

        def run(*cmd, **_):
            text = " ".join(str(part) for part in cmd)
            def hits(key):
                return text.startswith(key) and (len(text) == len(key) or text[len(key)] == " ")
            # on an argument boundary, so `...tools factory` does not win over `...tools factory_drive`
            found = next((value for key, value in sorted((results or {}).items(), key=lambda kv: len(kv[0]),
                                                         reverse=True) if hits(key)), None)
            if found is None:  # no exact result: the defaults, then success
                found = next((value for key, value in defaults if text.startswith(key)),
                             subprocess.CompletedProcess([], 0, "", ""))
            if isinstance(found, Exception):
                raise found
            return found
        with mock.patch.object(cli, "run", side_effect=run) as ran, contextlib.redirect_stdout(printed):
            cli.cmd_update([])
        return [call.args for call in ran.call_args_list], printed.getvalue()

    def test_a_change_to_day_desk_updates_its_profile_and_an_unrelated_one_does_not(self):
        commands, _ = self.deploy_with("tenant/hermes/skills/software-factory/SKILL.md", "new skill\n")
        self.assertEqual(commands[:2], [("hermes", "profile", "show", "day-desk"),
                                        ("hermes", "profile", "update", "day-desk", "-y")])
        self.assertEqual(commands[2][:5], ("hermes", "-p", "day-desk", "config", "get"))
        commands, _ = self.deploy_with("notes.txt", "x\n")
        self.assertEqual(commands, [])

    def test_a_change_to_the_sandbox_rebuilds_day_desks_image(self):
        commands, _ = self.deploy_with("tenant/hermes/sandbox/Dockerfile", "FROM scratch\n")
        self.assertIn(("docker", "build", "-q", "-t", "day-desk-sandbox:1",
                       str(self.deploy / "tenant" / "hermes" / "sandbox")), commands)

    def test_a_config_change_removes_the_driving_tool_list_the_token_replaces(self):
        commands, printed = self.deploy_with("tenant/hermes/config.yaml", "mcp_servers: {}\n", results={
            "hermes -p day-desk config get mcp_servers.factory_drive.tools.include": subprocess.CompletedProcess([], 0, '["factory_status"]', "")})
        self.assertIn(("hermes", "-p", "day-desk", "config", "unset",
                       "mcp_servers.factory_drive.tools.include"), commands)
        self.assertIn("removed mcp_servers.factory_drive.tools.include", printed)
        self.assertIn("merge any other change into it by hand", printed)

    def test_a_config_change_with_no_driving_tool_list_runs_no_unset(self):
        commands, printed = self.deploy_with("tenant/hermes/config.yaml", "mcp_servers: {}\n", results={
            "hermes -p day-desk config get mcp_servers.factory_drive.tools.include": subprocess.CompletedProcess([], 1, "", "Config key not set")})
        self.assertNotIn(("hermes", "-p", "day-desk", "config", "unset",
                          "mcp_servers.factory_drive.tools.include"), commands)
        self.assertNotIn("removed mcp_servers.factory_drive.tools.include", printed)

    def test_a_stale_owner_include_is_widened_in_place_and_a_repeat_touches_nothing(self):
        # The 260 regression: the live profile's owner include list came from before
        # #239's factory_approve_plan_allowance, so Day Desk discovered 4 of the 5 owner
        # tools and the deploy could not advance. 4 reviewed entries plus one unrelated
        # owner entry, which must survive untouched.
        stale = [tool for tool in self.include() if tool != "factory_approve_plan_allowance"]
        stale.append("factory_docker_status")
        merged = stale + ["factory_approve_plan_allowance"]  # the owner's entries stay first
        commands, printed = self.deploy_with("tenant/hermes/SOUL.md", "soul\n", results={
            "hermes -p day-desk config get mcp_servers.factory.tools --json":
                subprocess.CompletedProcess([], 0, json.dumps({"include": stale}), ""),
            "hermes -p day-desk config get mcp_servers.factory.tools.include --json":
                subprocess.CompletedProcess([], 0, json.dumps(merged), "")})
        self.assertIn(("hermes", "-p", "day-desk", "config", "set",
                       "mcp_servers.factory.tools.include", json.dumps(merged)), commands)
        self.assertIn("update: added factory_approve_plan_allowance to "
                      "mcp_servers.factory.tools.include", printed)
        self.assertIn("update: deployed", printed)
        sets = [command for command in commands if command[:5] == ("hermes", "-p", "day-desk",
                                                                 "config", "set")]
        self.assertEqual(sets, [("hermes", "-p", "day-desk", "config", "set",
                                 "mcp_servers.factory.tools.include", json.dumps(merged))])
        for secret in ("owner-token", "agent-token", "Bearer", "Authorization"):
            self.assertNotIn(secret, printed)
        # The unset of a hand-merged driving list is ADR-0048; writing one is not. The approval
        # tool must never reach the driving identity through any include.
        self.assertFalse([command for command in commands
                          if ("config set" in " ".join(command) and "factory_drive"
                              in " ".join(command))])
        commands, printed = self.deploy_with("tenant/hermes/SOUL.md", "more\n", results={  # next update
            "hermes -p day-desk config get mcp_servers.factory.tools --json":
                subprocess.CompletedProcess([], 0, json.dumps({"include": merged}), "")})
        self.assertEqual([command for command in commands if "config set" in " ".join(command)], [])
        self.assertIn("update: deployed", printed)

    def test_a_live_profile_with_no_owner_include_is_refused_not_narrowed(self):
        for key, done in (("a missing include",
                           subprocess.CompletedProcess([], 1, "",
                            "Config key not set: mcp_servers.factory.tools")),
                          ("a null include",
                           subprocess.CompletedProcess([], 0, '{"include": null}', ""))):
            with self.subTest(key):
                with self.assertRaisesRegex(SystemExit, "factory.tools.include.*will not overwrite"):
                    self.deploy_with("tenant/hermes/SOUL.md", key + "\n", results={
                        "hermes -p day-desk config get mcp_servers.factory.tools --json": done})
                # the deploy record stays at the baseline the update opened with
                self.assertNotIn("update: deployed", (cli.STATE / "deployed").read_text())

    def test_a_malformed_owner_include_is_refused_not_overwritten(self):
        with self.assertRaisesRegex(SystemExit, "cannot read mcp_servers.factory.tools.include"):
            self.deploy_with("tenant/hermes/SOUL.md", "soul\n", results={
                "hermes -p day-desk config get mcp_servers.factory.tools --json":
                    subprocess.CompletedProcess([], 0, '{"include": 42}', "")})
        self.assertNotIn("update: deployed", (cli.STATE / "deployed").read_text())

    def test_an_include_beside_an_exclude_is_refused(self):
        with self.assertRaisesRegex(SystemExit, "beside a tools.exclude filter.*will not overwrite"):
            self.deploy_with("tenant/hermes/SOUL.md", "soul\n", results={
                "hermes -p day-desk config get mcp_servers.factory.tools --json":
                    subprocess.CompletedProcess([], 0,
                     json.dumps({"include": self.include(), "exclude": ["*lesson*"]}), "")})
        self.assertNotIn("update: deployed", (cli.STATE / "deployed").read_text())

    def test_a_failed_include_write_keeps_the_record_and_the_next_update_repairs_it(self):
        stale = [tool for tool in self.include() if tool != "factory_approve_plan_allowance"]
        merged = stale + ["factory_approve_plan_allowance"]
        with self.assertRaisesRegex(SystemExit, "writing mcp_servers.factory.tools.include failed"):
            self.deploy_with("tenant/hermes/SOUL.md", "soul\n", results={
                "hermes -p day-desk config get mcp_servers.factory.tools --json":
                    subprocess.CompletedProcess([], 0, json.dumps({"include": stale}), ""),
                "hermes -p day-desk config set":
                    subprocess.CompletedProcess([], 1, "", "Error: hermes could not write the profile")})
        record = (cli.STATE / "deployed").read_text()
        self.assertNotIn("update: deployed", record)
        # A retry: the spare key the broken deploy could not write is verified by its readback now.
        commands, printed = self.deploy_with(results={
            "hermes -p day-desk config get mcp_servers.factory.tools --json":
                subprocess.CompletedProcess([], 0, json.dumps({"include": stale}), ""),
            "hermes -p day-desk config get mcp_servers.factory.tools.include --json":
                subprocess.CompletedProcess([], 0, json.dumps(merged), "")})
        self.assertIn(("hermes", "-p", "day-desk", "config", "set",
                       "mcp_servers.factory.tools.include", json.dumps(merged)), commands)
        self.assertNotEqual((cli.STATE / "deployed").read_text(), record)

    def test_an_include_write_that_does_not_take_keeps_the_record(self):
        stale = [tool for tool in self.include() if tool != "factory_approve_plan_allowance"]
        merged = stale + ["factory_approve_plan_allowance"]
        with self.assertRaisesRegex(SystemExit, "reads back.*not the written list"):
            self.deploy_with("tenant/hermes/SOUL.md", "soul\n", results={
                "hermes -p day-desk config get mcp_servers.factory.tools --json":
                    subprocess.CompletedProcess([], 0, json.dumps({"include": stale}), ""),
                "hermes -p day-desk config get mcp_servers.factory.tools.include --json":
                    subprocess.CompletedProcess([], 0, json.dumps({"include": stale}), "")})
        record = (cli.STATE / "deployed").read_text()
        self.assertNotIn("update: deployed", record)  # and the same commit deploys on a true readback
        commands, printed = self.deploy_with(results={
            "hermes -p day-desk config get mcp_servers.factory.tools --json":
                subprocess.CompletedProcess([], 0, json.dumps({"include": stale}), ""),
            "hermes -p day-desk config get mcp_servers.factory.tools.include --json":
                subprocess.CompletedProcess([], 0, json.dumps(merged), "")})
        self.assertIn("update: deployed", printed)

    def test_a_failing_profile_update_fails_the_deploy_loudly(self):
        failed = subprocess.CompletedProcess([], 1, "", "Error: no such profile 'day-desk'")
        with self.assertRaisesRegex(SystemExit, "hermes profile update failed at [0-9a-f]{7}: Error: no such profile"):
            self.deploy_with("tenant/hermes/SOUL.md", "soul\n", results={"hermes profile update": failed})

    def test_a_step_that_failed_runs_again_on_the_next_update(self):
        failed = subprocess.CompletedProcess([], 1, "", "Cannot connect to the Docker daemon")
        with self.assertRaisesRegex(SystemExit, "sandbox image build failed"):
            self.deploy_with("tenant/hermes/sandbox/Dockerfile", "FROM scratch\n", results={"docker build -q": failed})
        commands, printed = self.deploy_with()  # no new commit: the build that failed is still owed
        self.assertIn(("docker", "build", "-q", "-t", "day-desk-sandbox:1",
                       str(self.deploy / "tenant" / "hermes" / "sandbox")), commands)
        self.assertIn("update: deployed", printed)
        self.assertEqual(self.deploy_with(), ([], ""))  # and once it is done, nothing runs

    def test_a_retry_restarts_factory_mcp_only_when_the_worker_build_stopped_the_run_before(self):
        failed = subprocess.CompletedProcess([], 1, "", "Cannot connect to the Docker daemon")
        with self.assertRaises(SystemExit):
            self.deploy_with("tenant/hermes/sandbox/Dockerfile", "FROM scratch\n", results={"docker build -q": failed})
        self.assertEqual(len(self.restarts), 1)
        self.deploy_with()  # factory-mcp runs the new commit already
        self.assertEqual(len(self.restarts), 1)
        with self.assertRaises(SystemExit):
            self.deploy_with("worker/Dockerfile", "FROM scratch\n", results={"docker build -q": failed})
        self.assertEqual(len(self.restarts), 1)
        self.deploy_with()
        self.assertEqual(len(self.restarts), 2)

    def test_tests_that_fail_on_a_retry_stop_it(self):
        self.commit("tests/test_env.py", "import os, unittest\n\n\nclass T(unittest.TestCase):\n"
                    "    def test_env(self):\n        self.assertNotIn('FACTORY_TEST_BREAK', os.environ)\n")
        failed = subprocess.CompletedProcess([], 1, "", "Cannot connect to the Docker daemon")
        with self.assertRaises(SystemExit):
            self.deploy_with("tenant/hermes/sandbox/Dockerfile", "FROM scratch\n", results={"docker build -q": failed})
        record = (cli.STATE / "deployed").read_text()
        with mock.patch.dict(os.environ, {"FACTORY_TEST_BREAK": "1"}), \
                self.assertRaisesRegex(SystemExit, "tests fail"):
            self.deploy_with()
        self.assertEqual(((cli.STATE / "deployed").read_text(), len(self.restarts)), (record, 1))

    def test_a_missing_command_fails_the_deploy_with_a_message(self):
        with self.assertRaisesRegex(SystemExit, "hermes profile show failed at [0-9a-f]{7}: no hermes on PATH"):
            self.deploy_with("tenant/hermes/SOUL.md", "soul\n",
                             results={"hermes profile show": FileNotFoundError("hermes")})

    def test_a_profile_installed_from_another_checkout_is_not_updated(self):
        elsewhere = subprocess.CompletedProcess([], 0, "Installed from: /Users/me/dev/software-factory/tenant/hermes\n", "")
        with self.assertRaisesRegex(SystemExit, "installed from /Users/me/dev/software-factory/tenant/hermes, not "):
            self.deploy_with("tenant/hermes/SOUL.md", "soul\n", results={"hermes profile show": elsewhere})

    def test_an_update_that_changed_the_profile_verifies_the_tools_both_tokens_list(self):
        commands, printed = self.deploy_with("tenant/hermes/SOUL.md", "soul\n")
        self.assertIn(("hermes", "-p", "day-desk", "mcp", "test", "factory_drive"), commands)
        self.assertIn(("hermes", "-p", "day-desk", "mcp", "test", "factory"), commands)
        self.assertEqual(set(self.mcp.asked), {"Bearer agent-token", "Bearer owner-token"})
        # Nothing private leaks and nothing private is touched: profile show and update only, the
        # factory-owned unset it already knows, no install, no model, gateway or config writes.
        touched = {" ".join(map(str, command)) for command in commands if command[:1] == ("hermes",)}
        self.assertFalse([c for c in touched if c.startswith("hermes profile install")
                          or c.startswith("hermes -p day-desk config set")
                          or c.startswith("hermes -p day-desk model")
                          or c.startswith("hermes -p day-desk gateway")])
        for secret in ("owner-token", "agent-token", "Authorization", "Bearer"):
            self.assertNotIn(secret, printed)
        self.assertIn("update: deployed", printed)

    def test_a_driving_tool_the_live_profile_misses_keeps_the_record_until_the_next_update(self):
        missed = sorted(AGENT_TOOLS - {"factory_resolve_human"})
        with self.assertRaisesRegex(SystemExit, "factory_resolve_human"):
            self.deploy_with("tenant/hermes/SOUL.md", "soul\n", results={
                "hermes -p day-desk mcp test factory_drive":
                    subprocess.CompletedProcess([], 0, probe_output(missed), "")})
        record = (cli.STATE / "deployed").read_text()  # the previous baseline is kept
        self.assertEqual(len(self.restarts), 1)  # but the already-verified code runs on factory-mcp
        commands, printed = self.deploy_with()  # a fixed profile: the same commit deploys
        self.assertIn("update: deployed", printed)
        self.assertIn(("hermes", "-p", "day-desk", "mcp", "test", "factory_drive"), commands)
        self.assertNotEqual((cli.STATE / "deployed").read_text(), record)

    def test_an_owner_tool_the_live_profile_misses_fails_the_deploy_loudly(self):
        include = self.include()
        missed = [tool for tool in include if tool != "factory_approve_lesson"]
        with self.assertRaisesRegex(SystemExit, "factory_approve_lesson"):
            self.deploy_with("tenant/hermes/SOUL.md", "soul\n", results={
                "hermes -p day-desk mcp test factory":
                    subprocess.CompletedProcess([], 0, probe_output(missed), "")})

    def test_a_profile_update_that_blinds_both_servers_fails_before_any_deploy_record(self):
        with self.assertRaisesRegex(SystemExit, "the live tool discovery check failed"):
            self.deploy_with("tenant/hermes/SOUL.md", "soul\n", results={
                "hermes -p day-desk mcp test": subprocess.CompletedProcess([], 1, "", "no such profile")})
        self.assertNotIn("update: deployed", (cli.STATE / "deployed").read_text())

    def test_a_discovery_answer_the_update_cannot_read_fails_the_deploy_too(self):
        count = len(AGENT_TOOLS)  # the list, not its size, is the fact; #239 added a driving tool
        with self.assertRaisesRegex(SystemExit, rf"discovers 0 of the {count} factory_drive tools"):
            self.deploy_with("tenant/hermes/SOUL.md", "soul\n", results={
                "hermes -p day-desk mcp test":
                    subprocess.CompletedProcess([], 0, "Profile: day-desk\nnot json\n", "")})

    def test_a_live_filter_that_hides_a_discovered_tool_refuses_the_deploy(self):
        with self.assertRaisesRegex(SystemExit, "factory_resolve_human"):
            self.deploy_with("tenant/hermes/SOUL.md", "soul\n", results={
                "hermes -p day-desk config get mcp_servers.factory_drive.tools --json":
                    subprocess.CompletedProcess([], 0, '{"exclude": ["*resolve*"]}', "")})

    def test_live_include_patterns_take_precedence_over_exclude_like_hermes(self):
        _, printed = self.deploy_with("tenant/hermes/SOUL.md", "soul\n", results={
            "hermes -p day-desk config get mcp_servers.factory_drive.tools --json":
                subprocess.CompletedProcess([], 0, '{"include": "factory_*", "exclude": ["*"]}', "")})
        self.assertIn("update: deployed", printed)

    def test_an_unreadable_live_filter_refuses_the_deploy(self):
        with self.assertRaisesRegex(SystemExit, "cannot read factory_drive tool filters"):
            self.deploy_with("tenant/hermes/SOUL.md", "soul\n", results={
                "hermes -p day-desk config get mcp_servers.factory_drive.tools --json":
                    subprocess.CompletedProcess([], 1, "", "install failure")})

    def test_the_endpoint_keeps_its_request_history_to_itself(self):
        """Each endpoint instance records only its own requests: the missing-token deploy above
        sent its owner request on self.mcp, and a fresh endpoint sees none of it (class-level
        `asked` leaked one server's history into the next server's, which made a later deploy
        report requests it had never made)."""
        settings = dict(cli.SETTINGS)
        settings.pop("FACTORY_AGENT_TOKEN")  # the missing-token case: agent never authenticates
        with mock.patch.object(cli, "SETTINGS", settings), self.assertRaisesRegex(
                SystemExit, "lists no tools for the factory_drive token"):
            self.deploy_with("tenant/hermes/SOUL.md", "soul\n")
        second = McpEndpoint(("127.0.0.1", 0), McpRequest)
        second.listings = self.mcp.listings
        threading.Thread(target=second.serve_forever, daemon=True).start()
        self.addCleanup(second.shutdown)
        self.assertEqual(second.asked, ())  # the first server's requests stay with it

    def test_service_env_still_takes_the_environment_then_the_settings_then_the_secrets(self):
        self.config.mkdir(parents=True, exist_ok=True)
        (self.config / "secrets").write_text("FACTORY_AGENT_TOKEN=secrets-dummy\n")
        env = {"FACTORY_MCP_TOKEN": "env-dummy", "FACTORY_AGENT_TOKEN": "env-agent-dummy"}
        with mock.patch.dict(os.environ, env), mock.patch.object(
                cli, "SETTINGS", {"FACTORY_MCP_TOKEN": "settings-dummy"}):
            merged = cli.service_env()
        self.assertEqual((merged["FACTORY_MCP_TOKEN"], merged["FACTORY_AGENT_TOKEN"]),
                         ("settings-dummy", "secrets-dummy"))  # env < settings < secrets

    def test_an_unset_token_cannot_be_verified_and_its_endpoint_records_nothing(self):
        settings = dict(cli.SETTINGS)  # the one with both tokens, from setUp's patches
        settings.pop("FACTORY_AGENT_TOKEN")
        with mock.patch.object(cli, "SETTINGS", settings), self.assertRaisesRegex(
                SystemExit, "lists no tools for the factory_drive token"):
            self.deploy_with("tenant/hermes/SOUL.md", "soul\n")
        self.assertNotIn("update: deployed", (cli.STATE / "deployed").read_text())
        self.assertNotIn("Bearer agent-token", self.mcp.asked)  # no agent request; the owner check runs first

    def test_mcp_tools_answers_the_names_a_token_lists_and_none_when_it_cannot(self):
        self.assertEqual(cli.mcp_tools("agent-token"), sorted(AGENT_TOOLS))
        self.assertEqual(cli.mcp_tools("owner-token"), sorted(self.include()))
        self.assertEqual(cli.mcp_tools("not-a-token"), None)  # unauthorized
        self.assertEqual(cli.mcp_tools(""), None)  # an unset token never reaches the endpoint
        self.assertEqual(cli.mcp_tools(None), None)
        with mock.patch.object(cli, "PORT", cli.PORT + 1):  # nothing listens there
            self.assertEqual(cli.mcp_tools("agent-token"), None)

    def test_a_failing_commit_is_not_deployed(self):
        old = self.head()
        self.commit("tests/test_ok.py", PASS.replace("True)", "False)"))
        git(self.dev, "push", "-q", "origin", "HEAD:main")
        with self.assertRaisesRegex(SystemExit, r"tests fail[\s\S]*False is not true"):  # and why
            cli.cmd_update([])
        self.assertEqual((self.head(), self.restarts), (old, []))

    def test_a_merged_and_deleted_branch_is_followed_to_main(self):
        git(self.dev, "checkout", "-q", "-b", "feature")
        self.commit("feature.txt", "f\n")
        git(self.dev, "push", "-q", "origin", "feature")
        git(self.deploy, "fetch", "-q", "origin")
        git(self.deploy, "checkout", "-q", "-b", "feature", "origin/feature")
        git(self.dev, "checkout", "-q", "main")
        merged = self.commit("feature.txt", "f\n", message="Feature (#1)")  # a squash merge
        git(self.dev, "push", "-q", "origin", "HEAD:main", ":feature")
        cli.cmd_update([])
        self.assertEqual((self.head(), len(self.restarts)), ((merged, "main"), 1))

    def test_a_checkout_with_local_changes_is_left_alone(self):
        (self.deploy / "tests" / "test_ok.py").write_text(PASS + "# local edit\n")
        self.commit("notes.txt", "x\n")
        git(self.dev, "push", "-q", "origin", "HEAD:main")
        with self.assertRaisesRegex(SystemExit, "local changes"):
            cli.cmd_update([])
        self.assertIn("# local edit", (self.deploy / "tests" / "test_ok.py").read_text())



class AddRepoTest(unittest.TestCase):
    def add_repo(self, ready):
        """The tools add-repo called, and how it exited. It must run no command of its own."""
        calls = []

        def call(tool, arguments=None, timeout=60):
            calls.append((tool, timeout))
            return {"repo": "o/r", "ready": ready, "added": True,
                    "checks": [{"id": "gate_workflow", "ok": ready, "detail": "d", "fix": "f"}]}

        with mock.patch.object(cli, "call", call), mock.patch("builtins.print"), \
                mock.patch.object(cli, "run", side_effect=AssertionError("add-repo ran a command")):
            try:
                cli.cmd_add_repo(["o/r"])
            except SystemExit as stopped:
                return calls, str(stopped)
        return calls, None

    def test_a_ready_repo_is_enabled_through_the_tools(self):
        # enabling clones the worker checkout, which may take up to 900s
        self.assertEqual(self.add_repo(ready=True),
                         ([("factory_repo_check", 60), ("factory_add_repo", 960)], None))

    def test_a_failing_check_stops_it_before_the_repo_is_enabled(self):
        calls, stopped = self.add_repo(ready=False)
        self.assertEqual(calls, [("factory_repo_check", 60)])
        self.assertIn("o/r is not ready", stopped)


class WatchTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(cli, "STATE", Path(tmp.name))
        patcher.start()
        self.addCleanup(patcher.stop)

    def watch(self, *workitems, driving=False, command=None):
        """What one cron run prints, given the WorkItems factory_status returns. Each is (id, state,
        blockedOn) or a full row. `driving`: the secrets hold FACTORY_AGENT_TOKEN."""
        status = {"workItems": [w if isinstance(w, dict) else {"id": w[0], "state": w[1], "blockedOn": w[2]}
                                for w in workitems]}
        secrets = {"FACTORY_AGENT_TOKEN": "a"} if driving else {}
        printed = io.StringIO()
        with mock.patch.object(cli, "call", lambda tool, arguments=None: status), \
                mock.patch.object(cli, "read_env", lambda path: secrets), contextlib.redirect_stdout(printed):
            (command or cli.cmd_watch)([])
        return printed.getvalue()

    def test_while_day_desk_drives_the_owner_hears_only_what_it_may_not_answer_or_left_too_long(self):
        now = time.time()
        stamp = lambda ago: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - ago))
        fresh = {"id": "o/r#7", "state": "blocked", "blockedOn": "verify exited 1", "agentMayResolve": True,
                 "updatedAt": stamp(60)}
        answered = {"id": "o/r#8", "state": "human_waiting", "blockedOn": "HUMAN", "agentMayResolve": False,
                    "updatedAt": stamp(60)}
        printed = self.watch(fresh, answered, driving=True)
        self.assertNotIn("o/r#7", printed)  # Day Desk answers it first
        self.assertIn("o/r#8 is human_waiting: HUMAN", printed)  # Day Desk answered it once already
        self.assertIn("o/r#7 is blocked", self.watch(dict(fresh, updatedAt=stamp(1801)), answered, driving=True))
        self.assertIn("o/r#7 is blocked", self.watch(dict(fresh, blockedOn="new reason")))  # not driving

    def test_the_drive_queue_lists_only_what_day_desk_may_still_answer(self):
        rows = [{"id": "o/r#7", "state": "blocked", "blockedOn": "verify exited 1", "agentMayResolve": True},
                {"id": "o/r#8", "state": "human_waiting", "blockedOn": "HUMAN", "agentMayResolve": False},
                {"id": "o/r#9", "state": "implementing", "blockedOn": None}]
        self.assertEqual(self.watch(*rows, command=cli.cmd_drive_queue), "o/r#7 is blocked: verify exited 1\n")

    def test_an_exhausted_workitem_reaches_the_owner_whether_or_not_day_desk_drives_and_once(self):
        exhausted = {"id": "o/r#9", "state": "exhausted", "blockedOn": "4 worker passes used"}
        self.assertIn("o/r#9 is exhausted: 4 worker passes used", self.watch(exhausted, driving=True))
        self.assertEqual(self.watch(exhausted, driving=True), "")  # said once
        other = {"id": "o/r#10", "state": "exhausted", "blockedOn": "4 worker passes used"}
        self.assertIn("o/r#10 is exhausted: 4 worker passes used", self.watch(other, driving=False))
        self.assertEqual(self.watch(other, driving=False), "")  # said once

    def test_the_drive_queue_never_lists_an_exhausted_workitem(self):
        printed = self.watch({"id": "o/r#9", "state": "exhausted", "blockedOn": "4 worker passes used"},
                             command=cli.cmd_drive_queue)
        self.assertEqual(printed, "")

    def test_each_escalation_is_reported_once(self):
        blocked = ("o/r#7", "blocked", "verify exited 1")
        first = self.watch(blocked, ("o/r#8", "implementing", None))
        self.assertIn("o/r#7 is blocked: verify exited 1", first)
        self.assertNotIn("o/r#8", first)
        self.assertEqual(self.watch(blocked), "")  # nothing new, so Hermes sends nothing
        second = self.watch(blocked, ("o/r#9", "human_waiting", "pr-gatekeeper decided HUMAN"))
        self.assertEqual(second.splitlines()[0], "o/r#9 is human_waiting: pr-gatekeeper decided HUMAN")
        self.assertNotIn("o/r#7", second)
        self.watch()  # the owner answered both
        self.assertIn("o/r#7 is blocked", self.watch(blocked))  # blocked again is a new escalation

    def test_it_fails_loudly_when_factory_mcp_is_down(self):
        with socket.socket() as probe:  # a port with nothing listening on it
            probe.bind(("127.0.0.1", 0))
            closed = probe.getsockname()[1]
        with mock.patch.object(cli, "PORT", closed), \
                mock.patch.object(cli, "read_env", lambda path: {"FACTORY_MCP_TOKEN": "t"}), \
                self.assertRaisesRegex(SystemExit, "factory-mcp is not running"):
            cli.cmd_watch([])


class ReportTest(unittest.TestCase):
    NONE_ENDED = {"ended": 0, "autonomous": 0, "unknown": 0, "rate": None}
    EMPTY = {"since": "2026-09-28T03:30:00Z", "until": "2026-10-05T03:30:00Z",
             "autonomy": NONE_ENDED, "autonomyWeekBefore": NONE_ENDED, "escalated": 0,
             "topReasons": [], "newCandidates": [], "notWorking": []}

    def report(self, **numbers):
        """The lines `factory report` prints, given what factory_report returns. It reads only."""
        calls, printed = [], io.StringIO()

        def call(tool, arguments=None):
            calls.append(tool)
            return dict(self.EMPTY, **numbers)

        with mock.patch.object(cli, "call", call), contextlib.redirect_stdout(printed):
            cli.cmd_report([])
        self.assertEqual(calls, ["factory_report"])
        return printed.getvalue().splitlines()

    def test_a_week_where_nothing_ended_or_escalated_is_one_line(self):
        self.assertEqual(self.report(),
                         ["Factory week 2026-09-28 to 2026-10-05: nothing ended and nothing escalated."])

    def test_a_week_gives_both_autonomy_rates_and_the_top_reasons(self):
        lines = self.report(autonomy={"ended": 4, "autonomous": 3, "unknown": 0, "rate": 0.75},
                            escalated=3, topReasons=[{"reason": "pr-gatekeeper decided HUMAN", "count": 2},
                                                     {"reason": "verify\n exited 1", "count": 1}])
        self.assertEqual(lines, [
            "Factory week 2026-09-28 to 2026-10-05",
            "Autonomy: 3 of 4 ended WorkItems needed no person (75%). The week before: nothing ended.",
            "Went to a person: 3. Top reasons:",
            "  2x pr-gatekeeper decided HUMAN",
            "  1x verify exited 1"])

    def test_a_full_week_fits_one_telegram_message(self):
        long = "a finding that runs on\n" * 30
        lines = self.report(
            autonomy={"ended": 9, "autonomous": 2, "unknown": 1, "rate": 0.222},
            autonomyWeekBefore={"ended": 3, "autonomous": 3, "unknown": 2, "rate": 1.0},
            escalated=40, topReasons=[{"reason": long, "count": 10}] * 3,
            newCandidates=[{"repo": "o/r", "fingerprint": f"f{n}", "problem": long, "workItems": 2}
                           for n in range(12)],
            notWorking=[{"repo": "o/r", "text": long, "sent": 4, "recurred": 3}] * 12)
        self.assertLessEqual(len(lines), 20)
        self.assertLessEqual(max(len(line) for line in lines), cli.REPORT_WIDTH)
        self.assertIn("New lesson candidates: 12", lines)
        self.assertIn("Lessons not working: 12", lines)
        self.assertIn("2 of 9 ended WorkItems needed no person (22%), and 1 rebuilt from GitHub", lines[1])


class LessonsPathTest(unittest.TestCase):
    """`factory lessons-path`: the lessons file the service resolves, verified, no credentials."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config = Path(tmp.name) / "config"
        self.config.mkdir()  # tests write lessons.json into it

    def printed(self, **patches):
        out = io.StringIO()
        with mock.patch.object(cli, "CONFIG", self.config), \
                mock.patch.object(cli, "SETTINGS", {}), \
                mock.patch.object(cli, "read_env", lambda path: {}), \
                mock.patch.object(cli, "run", side_effect=AssertionError("no command may run")), \
                mock.patch.dict(os.environ, patches, clear=False), \
                contextlib.redirect_stdout(out):
            for key in ("FACTORY_LESSONS_FILE", "FACTORY_MCP_TOKEN"):
                if key not in patches:  # no credential may be required and no stray path leak in
                    os.environ.pop(key, None)
            try:
                cli.cmd_lessons_path([])
            except SystemExit as stopped:
                return out.getvalue(), str(stopped)
        return out.getvalue(), None

    def test_the_service_default_resolves_and_reports_its_lessons(self):
        lesson = '{"repo": "o/r", "fingerprint": "f", "text": "t", "approved_by": "a",'
        (self.config / "lessons.json").write_text(
            '{"version": 1, "lessons": [' + lesson + ' "approved_at": "2026-01-01T00:00:00Z"}]}')
        out, stopped = self.printed()
        self.assertIsNone(stopped)
        self.assertEqual(out.strip(), f"{self.config / 'lessons.json'} (1 lesson)")

    def test_the_environment_overrides_the_default(self):
        path = self.config / "elsewhere.json"
        out, stopped = self.printed(FACTORY_LESSONS_FILE=str(path))
        self.assertIsNone(stopped)
        self.assertEqual(out.strip(), f"{path} (0 lessons)")

    def test_no_lessons_file_yet_still_says_the_resolved_path(self):
        out, stopped = self.printed()
        self.assertEqual(out.strip(), f"{self.config / 'lessons.json'} (0 lessons)")

    def test_a_broken_lessons_file_fails_loudly_and_names_it(self):
        (self.config / "lessons.json").write_text("{oops")
        out, stopped = self.printed()
        self.assertEqual(out, "")  # it printed nothing before the loud failure
        self.assertIn("not JSON", stopped)

    def test_serve_resolves_the_same_path_the_command_prints(self):
        """The service and the check must never disagree on where the lessons live."""
        env = {}
        with mock.patch.object(cli, "CONFIG", self.config):
            env.setdefault("FACTORY_LESSONS_FILE", str(cli.lessons_path(env)))
        self.assertEqual(env["FACTORY_LESSONS_FILE"], str(self.config / "lessons.json"))


class StatusTest(unittest.TestCase):
    def status(self, **summary):
        """The lines `factory status` prints, given what factory_status returns."""
        summary = dict({"workItems": [], "autonomy": {"autonomous": 0, "ended": 0}}, **summary)
        printed = io.StringIO()
        with mock.patch.object(cli, "call", lambda tool, arguments=None: summary), \
                contextlib.redirect_stdout(printed):
            cli.cmd_status([])
        return printed.getvalue().splitlines()

    def test_each_lesson_gets_a_line_and_one_that_is_not_working_is_flagged(self):
        long = "Every new endpoint carries a test for its 401 path, and its 403 path too."
        lines = self.status(lessonEffect={
            "o/r:fp1": {"repo": "o/r", "text": long, "sent": 4, "recurred": 3, "notWorking": True},
            "o/r:fp2": {"repo": "o/r", "text": "Name tests for behavior", "sent": 2, "recurred": 0,
                        "notWorking": False}})
        self.assertEqual(lines[-2:], [f"lesson [o/r] {long[:60]}: sent on 4, recurred on 3  NOT WORKING",
                                      "lesson [o/r] Name tests for behavior: sent on 2, recurred on 0"])

    def test_a_workitem_that_waits_on_another_issue_names_it(self):
        lines = self.status(workItems=[{"id": "o/r#201", "state": "ready", "waitingOn": 200},
                                       {"id": "o/r#200", "state": "implementing", "waitingOn": None}])
        self.assertTrue(lines[0].endswith("   waiting on #200"))
        self.assertNotIn("waiting", lines[1])

    def test_a_lessons_file_the_factory_cannot_read_is_named(self):
        lines = self.status(lessonsError="lessons.json is not JSON: Expecting value")
        self.assertEqual(lines[-1], "lessons: lessons.json is not JSON: Expecting value")


class Relay(BaseHTTPRequestHandler):
    """agent-relay's task API: one running task for each of two agents."""
    seen = []  # (method, path, Authorization header or None)

    def log_message(self, *args):
        pass

    def reply(self, body):
        data = json.dumps(body).encode()
        self.send_response(200)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        Relay.seen.append(("GET", self.path, self.headers.get("Authorization")))
        self.reply({"tasks": [{"id": "t1", "agentId": "worker"}, {"id": "t2", "agentId": "other"}]})

    def do_DELETE(self):
        Relay.seen.append(("DELETE", self.path, self.headers.get("Authorization")))
        self.reply({"id": self.path.rsplit("/", 1)[-1], "status": "cancelled"})


class StopTest(unittest.TestCase):
    def setUp(self):
        Relay.seen = []
        server = ThreadingHTTPServer(("127.0.0.1", 0), Relay)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config, self.token_file = Path(tmp.name), Path(tmp.name, "relay-token")
        idle = subprocess.CompletedProcess([], 1, "", "")  # no service, listener or container
        for p in (mock.patch.object(cli, "RELAY", f"http://127.0.0.1:{server.server_port}"),
                  mock.patch.object(cli, "SETTINGS", {"FACTORY_WORKERS": "worker:local"}),
                  mock.patch.object(cli, "CONFIG", self.config),
                  mock.patch.object(cli, "run", lambda *cmd, check=False: idle),
                  mock.patch.dict(os.environ, {"AGENT_RELAY_TOKEN_FILE": str(self.token_file)})):
            p.start()
            self.addCleanup(p.stop)
        os.environ.pop("AGENT_RELAY_TOKEN", None)  # the patch restores it

    def stop(self):
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            cli.cmd_stop([])
        return printed.getvalue()

    def test_stop_sends_the_relay_token_from_its_file(self):
        self.token_file.write_text("file-token\n")
        self.assertIn("cancelled worker task t1", self.stop())
        self.assertEqual(Relay.seen, [("GET", "/v1/tasks?status=running", "Bearer file-token"),
                                      ("DELETE", "/v1/tasks/t1", "Bearer file-token")])

    def test_stop_sends_the_token_factory_serve_gives_factory_mcp(self):
        (self.config / "secrets").write_text("AGENT_RELAY_TOKEN=secret-token\n")
        self.token_file.write_text("file-token\n")
        printed = self.stop()
        self.assertEqual({auth for _, _, auth in Relay.seen}, {"Bearer secret-token"})
        self.assertNotIn("secret-token", printed)



class BenchTest(unittest.TestCase):
    HIDDEN = ("import importlib, os, unittest\n\n\nclass T(unittest.TestCase):\n    def test(self):\n"
              "        self.assertEqual(importlib.import_module(os.environ['BENCH_MODULE']).answer(), 42)\n")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root, self.created, self.real_run = Path(tmp.name), [], subprocess.run
        for task in ("alpha", "beta"):
            (self.root / "bench" / task).mkdir(parents=True)
            (self.root / "bench" / task / "issue.md").write_text(
                f"# Task {task}\n\nAdd `answer()` in `{{dir}}/{task}.py`, importable as `bench.{{run}}.{task}`.\n\n"
                f"## Acceptance\n- answer gives 42 — done when: `tests/test_{{run}}_{task}.py` passes\n")
            (self.root / "bench" / task / "test_hidden.py").write_text(self.HIDDEN)
        for name, value in (("BENCH", self.root / "bench"), ("STATE", self.root / "state")):
            patcher = mock.patch.object(cli, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_fake(self, argv, **kwargs):
        """gh and docker at the process boundary: the hidden tests run on this machine instead."""
        if argv[:3] == ["gh", "issue", "create"]:
            self.created.append((argv[argv.index("--title") + 1], kwargs["input"]))
            return subprocess.CompletedProcess(argv, 0, f"https://github.com/o/r/issues/{len(self.created)}\n", "")
        if argv[:3] == ["gh", "api", "repos/o/r/zipball/m1"]:
            archive = io.BytesIO()
            with zipfile.ZipFile(archive, "w") as files:  # alpha is right, beta is wrong
                files.writestr("o-r-m1/bench/r0101/alpha.py", "def answer():\n    return 42\n")
                files.writestr("o-r-m1/bench/r0101/beta.py", "def answer():\n    return 41\n")
            return subprocess.CompletedProcess(argv, 0, archive.getvalue(), b"")
        if argv[:2] == ["gh", "api"]:  # either PR: 10 lines added, 2 deleted, merged 30 minutes in
            pr = {"additions": 10, "deletions": 2, "merged_at": "2026-10-03T05:30:00Z", "merge_commit_sha": "m1"}
            return subprocess.CompletedProcess(argv, 0, json.dumps(pr).encode(), b"")
        self.assertEqual(argv[:2], ["docker", "run"])
        tree = argv[argv.index("-v") + 1].rsplit(":", 1)[0]
        self.docker = argv
        command = argv[argv.index("python3"):]  # after `timeout`, which macOS lacks
        env = dict(os.environ, **dict(argv[i + 1].split("=", 1) for i, flag in enumerate(argv) if flag == "-e"))
        return self.real_run(command, cwd=tree, env=env, capture_output=True, text=True)

    def bench(self, *args):
        printed = io.StringIO()
        workitems = {f"o/r#{n}": {"id": f"o/r#{n}", "state": "closed", "prNumber": 10 + n} for n in (1, 2)}
        history = {"o/r#1": [{"action": "result", "detail": {"commitSha": "c1"}},
                             {"action": "reconcile", "detail": {"headSha": "c1"}}],
                   "o/r#2": [{"action": "attempt_failed", "detail": {"to": "blocked"}}]}

        def call(tool, arguments=None):
            if arguments:
                wi = workitems[arguments["id"]]
                return {"workItem": wi, "passes": 1, "history": history[wi["id"]]}
            return {"workItems": list(workitems.values())}
        with mock.patch.object(cli.subprocess, "run", self.run_fake), mock.patch.object(cli, "call", call), \
                contextlib.redirect_stdout(printed):
            cli.cmd_bench(list(args))
        return printed.getvalue().splitlines()

    def test_a_run_files_one_issue_per_task_with_its_paths_and_workers(self):
        self.bench("o/r", "-w", "pi-glm-factory")
        run = json.loads(next((self.root / "state" / "bench").glob("r*.json")).read_text())
        self.assertEqual(run["issues"], {"alpha": 1, "beta": 2})
        title, body = self.created[0]
        self.assertEqual(title, f"Task alpha ({run['run']})")
        self.assertEqual(body, f"Add `answer()` in `bench/{run['run']}/alpha.py`, importable as "
                               f"`bench.{run['run']}.alpha`.\n\nWorker: pi-glm-factory\n\n## Acceptance\n"
                               f"- answer gives 42 — done when: `tests/test_{run['run']}_alpha.py` passes\n")

    def test_the_report_grades_the_merged_code_with_each_tasks_hidden_tests(self):
        (self.root / "state" / "bench").mkdir(parents=True)
        (self.root / "state" / "bench" / "r0101.json").write_text(json.dumps(
            {"run": "r0101", "repo": "o/r", "workers": [], "started": "2026-10-03T05:00:00Z",
             "issues": {"alpha": 1, "beta": 2}}))
        lines = self.bench("report", "r0101")
        self.assertEqual(lines[2].split(), ["alpha", "closed", "no", "1", "12", "30", "pass"])
        self.assertEqual(lines[3].split(), ["beta", "closed", "yes", "1", "12", "30", "fail"])
        self.assertEqual(lines[4], "merged with no person: 1/2; hidden tests pass: 1/2; worker passes: 2; "
                                   "median minutes to merge: 30")
        # The worker's code runs as a worker's verify does: no network, no capabilities, the host's uid.
        for flag in (["--network", "none"], ["--cap-drop", "ALL"], ["--security-opt", "no-new-privileges"],
                     ["--user", f"{os.getuid()}:{os.getgid()}"]):
            self.assertIn(flag, [self.docker[i:i + 2] for i in range(len(self.docker) - 1)])

    def test_a_second_run_in_the_same_second_is_refused(self):
        (self.root / "state" / "bench").mkdir(parents=True)
        (self.root / "state" / "bench" / "r0101120000.json").write_text("{}")
        stamp = time.strptime("2026-01-01 12:00:00", "%Y-%m-%d %H:%M:%S")
        with mock.patch.object(cli.time, "gmtime", lambda *_: stamp), self.assertRaises(SystemExit) as stopped:
            self.bench("o/r")
        self.assertIn("a run named r0101120000 exists", str(stopped.exception))
        self.assertEqual(self.created, [])


if __name__ == "__main__":
    unittest.main()
