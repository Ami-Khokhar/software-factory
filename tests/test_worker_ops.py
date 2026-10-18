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


if __name__ == "__main__":
    unittest.main()
