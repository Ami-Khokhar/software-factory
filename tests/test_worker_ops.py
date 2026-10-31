"""Worker health and relay reload operations stay within registered configuration."""
import json
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from factory_mcp import worker_ops
from factory_mcp.config import Config
from factory_mcp.mcp import ToolError
from factory_mcp.store import Store
from tests.fakes import FakeRelay


class WorkerOpsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.credential = self.root / "auth.json"
        self.credential.write_text("private auth fixture")
        self.registry = self.root / "agents.json"
        self.agent = {"id": "opencode-mimo-factory", "command": "python3", "args": [
            str(worker_ops.RUNNER), "--credential", f"{self.credential}:/tmp/auth",
            "--", "opencode", "run", "--model", "opencode/mimo-free"], "timeoutMs": 1500000}
        self.registry.write_text(json.dumps({"agents": [self.agent]}))
        self.config = Config(mcp_token="owner", repos=frozenset(), agents_file=str(self.registry))
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.relay = FakeRelay()
        self.health = {"schemaVersion": worker_ops.HEALTH_SCHEMA, "status": "ok",
                       "finishedAt": "2026-10-19T10:00:00Z", "events": [
                           {"ts": "2026-10-19T10:00:00Z", "event": "preflight", "tool": "opencode",
                            "status": "ok", "exitCode": 0, "retries": 0},
                           {"ts": "2026-10-19T10:00:01Z", "event": "probe", "tool": "opencode",
                            "status": "ok", "exitCode": 0, "retries": 0}]}

    def check(self):
        with mock.patch.object(worker_ops, "_image_id", return_value="sha256:" + "a" * 64), \
             mock.patch.object(worker_ops, "_run", return_value=(self.health, True)):
            return worker_ops.check(self.store, self.config, "opencode-mimo-factory", "Day Desk", agent=True)

    def test_a_health_check_persists_a_bounded_binding_without_dispatching_a_workorder(self):
        result = self.check()
        stored = self.store.worker_health("opencode-mimo-factory")
        self.assertEqual((result["recordStatus"], stored["status"]), ("healthy", "healthy"))
        self.assertRegex(stored["binding"], r"^[a-f0-9]{64}$")
        self.assertNotEqual(stored["binding"], json.dumps(self.agent))
        self.assertEqual(stored["validUntil"], result["validUntil"])
        remaining = (datetime.strptime(stored["validUntil"], "%Y-%m-%dT%H:%M:%SZ") -
                     datetime.strptime(stored["checkedAt"], "%Y-%m-%dT%H:%M:%SZ")).total_seconds()
        self.assertEqual(remaining, worker_ops.HEALTH_TTL)
        self.assertEqual(self.store.all_workorders(), [])
        self.assertEqual(self.relay.tasks, {})

    def test_a_failed_probe_is_persisted_as_quarantined_without_a_workorder(self):
        failed = {"schemaVersion": worker_ops.HEALTH_SCHEMA, "status": "provider_auth",
                  "finishedAt": "2026-10-19T10:00:00Z", "events": []}
        with mock.patch.object(worker_ops, "_image_id", return_value="sha256:" + "a" * 64), \
             mock.patch.object(worker_ops, "_run", return_value=(failed, True)):
            result = worker_ops.check(self.store, self.config, "opencode-mimo-factory", "owner")
        row = self.store.worker_health("opencode-mimo-factory")
        self.assertEqual((result["recordStatus"], row["status"]), ("quarantined", "quarantined"))
        self.assertEqual(json.loads(row["report"])["status"], "provider_auth")
        self.assertEqual(self.store.all_workorders(), [])

    def test_a_config_or_credential_metadata_change_replaces_the_health_binding(self):
        self.check()
        before = self.store.worker_health("opencode-mimo-factory")["binding"]
        self.agent["args"][-1] = "opencode/mimo-other"
        self.registry.write_text(json.dumps({"agents": [self.agent]}))
        self.check()
        model_changed = self.store.worker_health("opencode-mimo-factory")["binding"]
        self.assertNotEqual(before, model_changed)
        self.credential.write_text("a changed credential file size")
        self.check()
        self.assertNotEqual(model_changed, self.store.worker_health("opencode-mimo-factory")["binding"])

    def test_a_different_image_identity_replaces_the_health_binding(self):
        with mock.patch.object(worker_ops, "_image_id", side_effect=["sha256:" + "a" * 64,
                                                                      "sha256:" + "b" * 64]), \
             mock.patch.object(worker_ops, "_run", return_value=(self.health, True)):
            worker_ops.check(self.store, self.config, "opencode-mimo-factory", "owner")
            before = self.store.worker_health("opencode-mimo-factory")["binding"]
            worker_ops.check(self.store, self.config, "opencode-mimo-factory", "owner")
        self.assertNotEqual(before, self.store.worker_health("opencode-mimo-factory")["binding"])

    def test_secret_environment_keys_are_rejected_before_image_or_probe_processes(self):
        self.agent["args"] = [str(worker_ops.RUNNER), "--env", "MODEL_API_KEY=do-not-copy",
                              "--credential", f"{self.credential}:/tmp/auth",
                              "--", "opencode", "run", "--model", "opencode/mimo-free"]
        self.registry.write_text(json.dumps({"agents": [self.agent]}))
        with mock.patch.object(worker_ops, "_image_id") as image, mock.patch.object(worker_ops, "_run") as run:
            with self.assertRaisesRegex(ToolError, "secret-bearing"):
                worker_ops.check(self.store, self.config, "opencode-mimo-factory", "owner")
        image.assert_not_called()
        run.assert_not_called()

    def test_reload_calls_only_relay_reload_and_records_a_sanitized_status(self):
        result = worker_ops.reload(self.store, self.relay, "Day Desk", agent=True)
        self.assertEqual(result, {"status": "reloaded"})
        self.assertEqual(self.relay.reloads, 1)
        self.assertEqual(self.relay.tasks, {})
        self.assertEqual(self.store.global_history()[-1]["action"], "worker_registry_reload")

    def test_reload_failures_are_reduced_to_a_status_without_relay_error_text(self):
        self.relay.down = True
        result = worker_ops.reload(self.store, self.relay, "Day Desk", agent=True)
        self.assertEqual(result, {"status": "unavailable"})
        self.assertEqual(self.relay.tasks, {})
        self.assertNotIn("connection refused", json.dumps(self.store.global_history()[-1]))

    def test_probe_child_has_closed_stdin_and_timeout_cleanup_is_scoped_to_its_label(self):
        process = mock.Mock(pid=4321, returncode=0)
        process.communicate.side_effect = [subprocess.TimeoutExpired("probe", 390), ("", "")]
        with mock.patch.object(worker_ops.uuid, "uuid4", return_value=mock.Mock(hex="b" * 32)), \
             mock.patch.object(worker_ops.subprocess, "Popen", return_value=process) as popen, \
             mock.patch.object(worker_ops.os, "killpg") as killpg, \
             mock.patch.object(worker_ops, "_stop_probe_containers", return_value=True) as stop:
            report, cleanup = worker_ops._run(worker_ops._candidate(self.config, "opencode-mimo-factory"))
        self.assertEqual(report["status"], "unavailable")
        self.assertTrue(cleanup)
        self.assertIs(popen.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertTrue(popen.call_args.kwargs["start_new_session"])
        self.assertEqual(killpg.call_args.args[0], 4321)
        stop.assert_called_once_with("b" * 32)

    def test_child_output_is_reduced_to_the_health_schema_before_return(self):
        raw = dict(self.health, finishedAt="CANARY-SECRET", secret="CANARY-SECRET")
        raw["events"][1].update(ts="CANARY-SECRET", tool="raw-command", response="CANARY-SECRET")
        process = mock.Mock(pid=4322, returncode=0)
        process.communicate.return_value = (json.dumps(raw), "CANARY-SECRET")
        with mock.patch.object(worker_ops.uuid, "uuid4", return_value=mock.Mock(hex="e" * 32)), \
             mock.patch.object(worker_ops.subprocess, "Popen", return_value=process):
            report, cleanup = worker_ops._run(worker_ops._candidate(self.config, "opencode-mimo-factory"))
        rendered = json.dumps(report)
        self.assertTrue(cleanup)
        self.assertEqual(report["status"], "ok")
        self.assertNotIn("CANARY-SECRET", rendered)
        self.assertNotIn("response", rendered)
        self.assertEqual(report["events"][1]["tool"], "opencode")

    def test_health_child_success_is_parsed_without_returning_stderr(self):
        process = mock.Mock(pid=4323, returncode=0)
        process.communicate.return_value = (json.dumps(self.health), "CANARY-DIAGNOSTIC")
        with mock.patch.dict("os.environ", {"FACTORY_GITHUB_TOKEN": "CANARY-HOST-TOKEN"}), \
             mock.patch.object(worker_ops.uuid, "uuid4", return_value=mock.Mock(hex="f" * 32)), \
             mock.patch.object(worker_ops.subprocess, "Popen", return_value=process) as popen:
            report, cleanup = worker_ops._run(worker_ops._candidate(self.config, "opencode-mimo-factory"))
        self.assertTrue(cleanup)
        self.assertEqual(report["status"], "ok")
        self.assertNotIn("CANARY-DIAGNOSTIC", json.dumps(report))
        self.assertEqual(popen.call_args.args[0][1], str(worker_ops.HEALTH_SCRIPT))
        child_env = popen.call_args.kwargs["env"]
        self.assertNotIn("FACTORY_GITHUB_TOKEN", child_env)
        self.assertFalse(Path(child_env["FACTORY_HEALTH_SCRATCH_ROOT"]).exists())

    def test_unapproved_harness_flags_are_rejected_before_any_child_starts(self):
        self.agent["args"].extend(["--api-key", "DO-NOT-FORWARD"])
        self.registry.write_text(json.dumps({"agents": [self.agent]}))
        with mock.patch.object(worker_ops, "_image_id") as image, mock.patch.object(worker_ops, "_run") as run:
            with self.assertRaisesRegex(ToolError, "unsupported option"):
                worker_ops.check(self.store, self.config, "opencode-mimo-factory", "owner")
        image.assert_not_called()
        run.assert_not_called()

    def test_docker_cleanup_stops_only_ids_from_the_unique_probe_label(self):
        container = "c" * 64
        with mock.patch.object(worker_ops.subprocess, "run", side_effect=[
                subprocess.CompletedProcess([], 0, container + "\nunsafe\n", ""),
                subprocess.CompletedProcess([], 0, "", "")]) as run:
            self.assertTrue(worker_ops._stop_probe_containers("d" * 32))
        self.assertEqual(run.call_args_list[0].args[0][-1],
                         "label=factory-worker=health-" + "d" * 32)
        self.assertEqual(run.call_args_list[1].args[0], ["docker", "stop", "--time", "5", container])

    def activation_tools(self, agent=False):
        from factory_mcp.foreman import Foreman
        from factory_mcp import tools
        from tests.fakes import FakeGitHub
        foreman = Foreman(self.store, FakeGitHub(), self.config, self.relay)
        return tools.build(self.store, foreman, agent=agent)

    def activate(self, enabled=True, agent=False, **extra):
        return self.activation_tools(agent)["factory_set_worker_enabled"].fn({
            "worker": self.agent["id"], "enabled": enabled, "by": "owner",
            "note": "Enable the registered worker", **extra})

    def probe_processes(self):
        from contextlib import ExitStack
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(mock.patch.object(worker_ops.subprocess, "run", return_value=
            subprocess.CompletedProcess([], 0, "sha256:" + "a" * 64, "")))
        process = mock.Mock(returncode=0)
        process.communicate.return_value = (json.dumps(self.health), "")
        popen = stack.enter_context(mock.patch.object(worker_ops.subprocess, "Popen", return_value=process))
        return popen

    def test_registered_deepseek_template_flags_pass_the_public_health_operation(self):
        self.agent["id"] = "pi-deepseek-factory"
        self.agent["args"][self.agent["args"].index("--") + 1:] = [
            "pi", "--print", "--provider", "opencode-go", "--model", "deepseek-v4.1-flash",
            "--models", "opencode-go/deepseek-v4.1-flash", "--thinking", "high", "--no-session",
            "--no-extensions", "--no-skills", "--no-prompt-templates"]
        self.registry.write_text(json.dumps({"agents": [self.agent]}))
        popen = self.probe_processes()
        result = self.activation_tools()["factory_check_worker"].fn({"worker": self.agent["id"], "by": "owner"})
        self.assertEqual(result["recordStatus"], "healthy")
        self.activate()
        self.assertIn((self.agent["id"], "hosted"), worker_ops.effective_workers(self.store, self.config))
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(self.store.all_workorders(), [])

    def test_rejected_option_names_are_bounded_and_never_return_option_values(self):
        original = list(self.agent["args"])
        for option, diagnostic in (("--api-key", "--api-key"),
                                   ("--api-key=CANARY", "[redacted]"),
                                   ("CANARY-PAYLOAD", "[redacted]"),
                                   ("--" + "x" * 1000, "[redacted]")):
            self.agent["args"] = original + [option, "CANARY-SECRET"]
            self.registry.write_text(json.dumps({"agents": [self.agent]}))
            with self.assertRaises(ToolError) as error:
                self.activation_tools()["factory_check_worker"].fn({"worker": self.agent["id"], "by": "owner"})
            message = str(error.exception)
            self.assertIn(diagnostic, message)
            self.assertIn("approved registered template", message)
            self.assertNotIn("CANARY", message)
            self.assertLess(len(message), 220)

    def test_activation_requires_approval_and_current_healthy_binding(self):
        self.probe_processes()
        with self.assertRaisesRegex(ToolError, "owner approval"):
            self.activate(agent=True)
        with self.assertRaisesRegex(ToolError, "Day Desk"):
            self.activate(ownerAsked=True)
        with self.assertRaisesRegex(ToolError, "fresh successful"):
            self.activate()
        self.activation_tools()["factory_check_worker"].fn({"worker": self.agent["id"], "by": "owner"})
        healthy = self.store.worker_health(self.agent["id"])
        for changes in ({"status": "quarantined"}, {"validUntil": "2000-01-01T00:00:00Z"},
                        {"binding": "drifted"}, {"checkedAt": "2999-01-01T00:00:00Z"}):
            row = dict(healthy, **changes)
            self.store.put_worker_health(row["worker"], row["binding"], row["status"], row["checkedAt"],
                                         row["validUntil"], row["report"])
            with self.assertRaisesRegex(ToolError, "fresh successful"):
                self.activate()
        self.assertEqual(self.store.worker_activations(), [])

    def test_activation_preserves_membership_and_audits_without_touching_work(self):
        popen = self.probe_processes()
        self.activation_tools()["factory_check_worker"].fn({"worker": self.agent["id"], "by": "owner"})
        result = self.activate(agent=True, ownerAsked=True)
        self.assertTrue(result["enabled"])
        self.assertEqual(worker_ops.effective_workers(self.store, self.config),
                         (*self.config.workers, (self.agent["id"], "hosted")))
        detail = self.store.global_history()[-1]["detail"]
        self.assertEqual((detail["by"], detail["ownerAsked"], detail["note"]),
                         ("owner", True, "Enable the registered worker"))
        self.activate(False)
        self.assertEqual(worker_ops.effective_workers(self.store, self.config), self.config.workers)
        self.assertEqual(popen.call_count, 1)
        self.assertEqual(self.store.all_workorders(), [])
        self.assertEqual(self.relay.tasks, {})

    def test_active_membership_fails_closed_after_config_image_or_credential_drift(self):
        self.probe_processes()
        self.activation_tools()["factory_check_worker"].fn({"worker": self.agent["id"], "by": "owner"})
        self.activate()
        original = self.registry.read_text()
        self.agent["timeoutMs"] += 1
        self.registry.write_text(json.dumps({"agents": [self.agent]}))
        self.assertEqual(worker_ops.effective_workers(self.store, self.config), self.config.workers)
        self.registry.write_text(original)
        with mock.patch.object(worker_ops.subprocess, "run", return_value=
                subprocess.CompletedProcess([], 0, "sha256:" + "b" * 64, "")):
            self.assertEqual(worker_ops.effective_workers(self.store, self.config), self.config.workers)
        self.credential.write_text("changed credential size")
        self.assertEqual(worker_ops.effective_workers(self.store, self.config), self.config.workers)
        self.registry.write_text("broken registry")
        self.activate(False)  # deactivation remains available when repair is needed

    def test_health_for_a_changed_configuration_requires_explicit_reactivation(self):
        self.probe_processes()
        check = self.activation_tools()["factory_check_worker"]
        check.fn({"worker": self.agent["id"], "by": "owner"})
        self.activate()
        self.agent["timeoutMs"] += 1
        self.registry.write_text(json.dumps({"agents": [self.agent]}))
        check.fn({"worker": self.agent["id"], "by": "owner"})
        self.assertEqual(worker_ops.effective_workers(self.store, self.config), self.config.workers)
        self.activate()
        self.assertIn(self.agent["id"], dict(worker_ops.effective_workers(self.store, self.config)))

    def test_activation_cannot_hide_the_pinned_reviewer_family_in_a_worker_alias(self):
        self.agent["args"][-1] = "opencode/deepseek-v4.1-flash"
        self.registry.write_text(json.dumps({"agents": [self.agent]}))
        with self.assertRaisesRegex(ToolError, "model family"):
            self.activate()

    def test_durable_override_can_disable_a_configured_worker_after_restart(self):
        path = str(self.root / "activation.sqlite3")
        self.store.close()
        self.store = Store(path)
        self.activation_tools()["factory_set_worker_enabled"].fn({
            "worker": "codex", "enabled": False, "by": "owner", "note": "Disable codex"})
        self.store.close()
        self.store = Store(path)
        self.addCleanup(self.store.close)
        self.assertEqual(worker_ops.effective_workers(self.store, self.config), ())
        self.assertEqual(self.store.global_history()[-1]["action"], "worker_activation_changed")

    def test_an_unsuccessful_probe_event_cannot_authorize_activation(self):
        self.health["events"][1]["status"] = "provider_auth"
        self.probe_processes()
        result = self.activation_tools()["factory_check_worker"].fn({"worker": self.agent["id"], "by": "owner"})
        self.assertEqual(result["recordStatus"], "quarantined")
        with self.assertRaisesRegex(ToolError, "fresh successful"):
            self.activate()

    def test_unsafe_commands_mounts_and_options_never_start_a_probe(self):
        original = list(self.agent["args"])
        popen = self.probe_processes()
        for args in (["/bin/sh", "-c", "CANARY"],
                     [str(worker_ops.RUNNER), "--volume", "/:/host", "--", "pi"],
                     [str(worker_ops.RUNNER), "--credential", f"{self.credential}:/work", *original[3:]],
                     [str(worker_ops.RUNNER), "--credential", f"{self.credential}://work", *original[3:]],
                     [*original, "--extension", "CANARY"]):
            self.agent["args"] = args
            self.registry.write_text(json.dumps({"agents": [self.agent]}))
            with self.assertRaises(ToolError):
                self.activation_tools()["factory_check_worker"].fn({"worker": self.agent["id"], "by": "owner"})
        popen.assert_not_called()

    def test_activated_worker_routes_only_after_an_explicit_tick_and_keeps_reviewer_exclusion(self):
        from dataclasses import replace
        from factory_mcp.foreman import Foreman
        from tests.fakes import FakeGitHub
        self.probe_processes()
        self.activation_tools()["factory_check_worker"].fn({"worker": self.agent["id"], "by": "owner"})
        self.activate()
        github = FakeGitHub()
        github.add_issue("o/r", 7)
        foreman = Foreman(self.store, github, replace(self.config, repos=frozenset({"o/r"})), self.relay)
        foreman.intake("o/r", 7)
        before = self.store.get("o/r#7")
        foreman.set_worker("o/r#7", [self.agent["id"]], "owner")
        self.assertEqual(self.store.get("o/r#7"), before)
        self.assertEqual(self.relay.tasks, {})
        # A matching reviewer family still prevents dispatch for an activated worker.
        github.contents[("o/r", ".github/pr-gatekeeper.json")] = json.dumps({"reviewer_model": "mimo-v2"})
        wi = foreman.tick("o/r#7")["workItem"]
        self.assertEqual(wi["state"], "human_waiting")
        self.assertIn("reviewer family", wi["blockedOn"])
        self.assertEqual(self.relay.tasks, {})

    def test_activation_reaches_the_relay_only_when_the_owner_ticks_work(self):
        from dataclasses import replace
        from factory_mcp.foreman import Foreman
        from tests.fakes import FakeGitHub
        self.probe_processes()
        self.activation_tools()["factory_check_worker"].fn({"worker": self.agent["id"], "by": "owner"})
        self.activate()
        github = FakeGitHub()
        github.add_issue("o/r", 7)
        foreman = Foreman(self.store, github, replace(self.config, repos=frozenset({"o/r"})), self.relay)
        foreman.intake("o/r", 7)
        foreman.set_worker("o/r#7", [self.agent["id"]], "owner")
        self.assertEqual(self.relay.tasks, {})
        wi = foreman.tick("o/r#7")["workItem"]
        self.assertEqual((wi["state"], wi["worker"]), ("implementing", self.agent["id"]))
        self.assertEqual(len(self.relay.tasks), 1)
        self.assertEqual(self.relay.reloads, 1)

    def test_deactivation_blocks_a_pending_submission_without_buying_another_pass(self):
        from dataclasses import replace
        from factory_mcp.foreman import Foreman
        from tests.fakes import FakeGitHub
        self.probe_processes()
        self.activation_tools()["factory_check_worker"].fn({"worker": self.agent["id"], "by": "owner"})
        self.activate()
        github = FakeGitHub()
        github.add_issue("o/r", 7)
        foreman = Foreman(self.store, github, replace(self.config, repos=frozenset({"o/r"})), self.relay)
        foreman.intake("o/r", 7)
        foreman.set_worker("o/r#7", [self.agent["id"]], "owner")
        self.relay.down = True
        wi = foreman.tick("o/r#7")["workItem"]
        self.assertEqual(wi["state"], "dispatching")
        order = self.store.workorders("o/r#7")[0]
        self.relay.down = False
        self.activate(False)
        with self.assertRaisesRegex(ToolError, "activation changed"):
            foreman.submit(wi, order)
        self.assertEqual(len(self.store.workorders("o/r#7")), 1)
        self.assertEqual(self.relay.tasks, {})


if __name__ == "__main__":
    unittest.main()
