"""The provider health pilot: sandboxed, allowlisted reporting with provider errors classified."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import provider_health  # noqa: E402
import runner  # noqa: E402


class ClassifyTest(unittest.TestCase):
    def test_a_deadline_is_distinct_from_every_provider_error(self):
        # exit 124 says the sandbox's timeout ended the harness: nothing about 402, 429 or auth.
        self.assertEqual(provider_health.classify(124, "402 quota exceeded"), "harness_deadline")
        self.assertEqual(provider_health.classify(124, "429 rate limit"), "harness_deadline")

    def test_provider_errors_are_classified_without_spilling_the_text(self):
        self.assertEqual(provider_health.classify(1, "HTTP 429 too many requests, retry later"),
                         "provider_rate_limit")
        self.assertEqual(provider_health.classify(1, "error 402: insufficient balance"), "provider_quota")
        self.assertEqual(provider_health.classify(1, "401 unauthorized"), "provider_auth")
        self.assertEqual(provider_health.classify(1, "connection reset"), "provider_error")

    def test_an_exit_zero_with_no_error_words_is_ok(self):
        self.assertEqual(provider_health.classify(0, "ok"), "ok")

    def test_a_deadline_word_inside_a_provider_error_is_still_the_error(self):
        self.assertEqual(provider_health.classify(1, "timeout while requesting, 429 rate limit"),
                         "provider_rate_limit")


class SanitizeTest(unittest.TestCase):
    def test_only_allowlisted_fields_survive(self):
        dirty = {"ts": "2026-10-15T10:58:50Z", "event": "probe", "tool": "pi", "status": "ok",
                 "exitCode": 0, "retries": 0, "latencyMs": 812, "promptBytes": 39,
                 "responseBytes": 2, "diagnosticBytes": 0,
                 "raw": "full model response ...", "promptText": provider_health.PROBE,
                 "stdout": "WorkOrder:\n{\"factory.workorder/v1\": true}"}
        got = provider_health.sanitize(dirty)
        for field in ("raw", "promptText", "stdout"):
            self.assertNotIn(field, got)
        self.assertEqual(got["promptBytes"], 39)  # the byte count, never the bytes' text

    def test_unrecognized_names_and_statuses_carry_no_data(self):
        got = provider_health.sanitize({"ts": "x", "event": "custom-event", "status": "weird",
                                        "tool": "../etc/passwd", "exitCode": "many", "retries": 1})
        self.assertEqual(got["event"], "unknown")
        self.assertEqual(got["status"], "unknown")
        self.assertEqual(got["tool"], "unnamed")
        self.assertEqual(got["exitCode"], 0)
        self.assertNotIn("latencyMs", got)  # absent fields stay absent

    def test_a_report_is_parseable_json_whose_events_carry_only_allowlisted_keys(self):
        text = provider_health.report([{"ts": "x", "event": "probe", "tool": "pi", "status": "ok",
                                        "secret": "value", "rawResponse": "the whole reply"}], "ok")
        got = json.loads(text)
        self.assertEqual(got["schemaVersion"], "factory.health/v1")
        self.assertEqual(got["status"], "ok")
        for event in got["events"]:
            self.assertTrue(set(event) <= set(provider_health.ALLOWED_FIELDS))


class ProbeTest(unittest.TestCase):
    def _sandbox_returning(self, code, out, err, raises=None):
        fake = mock.Mock()
        fake.run.side_effect = raises or (lambda *a, **k: (code, out, err))
        return fake

    def test_a_valid_response_reports_ok_with_latency_and_byte_counts(self):
        sandbox = self._sandbox_returning(0, "ok\n", "")
        event, status = provider_health.probe(sandbox, ["pi"], 10, "scratch", "ok")
        self.assertEqual(status, "ok")
        self.assertEqual(event["event"], "probe")
        self.assertEqual(event["tool"], "pi")
        self.assertEqual(event["exitCode"], 0)
        self.assertEqual(event["responseBytes"], len("ok\n"))
        self.assertEqual(event["promptBytes"], len(provider_health.PROBE.encode()))
        self.assertTrue(0 <= event["latencyMs"] < 10_000)
        self.assertEqual(event["retries"], 0)
        # the prompt text itself never lands in the event
        self.assertNotIn(provider_health.PROBE, json.dumps(event))

    def test_an_unexpected_response_is_not_called_ok(self):
        sandbox = self._sandbox_returning(0, "hello, world\n", "")
        event, status = provider_health.probe(sandbox, ["pi"], 10, "scratch", "ok")
        self.assertEqual(status, "unexpected_response")
        self.assertEqual(event["responseBytes"], len("hello, world\n"))

    def test_a_deadline_is_reported_as_harness_deadline(self):
        sandbox = self._sandbox_returning(124, "", "timed out")
        event, status = provider_health.probe(sandbox, ["pi"], 10, "scratch", "ok")
        self.assertEqual(status, "harness_deadline")
        self.assertEqual(event["exitCode"], 124)

    def test_provider_errors_keep_only_the_class_name(self):
        stderr = "request failed: 429 rate limit for account keeper@example.test"
        sandbox = self._sandbox_returning(1, "", stderr)
        event, status = provider_health.probe(sandbox, ["pi"], 10, "scratch", "ok")
        self.assertEqual(status, "provider_rate_limit")
        self.assertNotIn("keeper@example.test", json.dumps(event))
        self.assertEqual(event["diagnosticBytes"], len(stderr))

    def test_an_unavailable_docker_daemon_is_reported_not_passed(self):
        sandbox = self._sandbox_returning(0, "", "", raises=runner.WorkerError(
            "docker is not available", runner.NO_DOCKER))
        event, status = provider_health.probe(sandbox, ["pi"], 10, "scratch", "ok")
        self.assertEqual(status, "unavailable")
        self.assertEqual(event["exitCode"], runner.NO_DOCKER)

    def test_an_opencode_argv_reaches_the_sandbox_with_auto(self):
        # The probe must run the harness exactly as a real pass does: the runner's own
        # harness_command adds --auto to an `opencode run` (ADR-0111), not the raw argv.
        sandbox = self._sandbox_returning(0, "ok\n", "")
        event, status = provider_health.probe(sandbox, ["opencode", "run", "--model", "m"],
                                              10, "scratch", "ok")
        self.assertEqual(status, "ok")
        self.assertEqual(sandbox.run.call_args.args[0],
                         ["opencode", "run", "--auto", "--model", "m", provider_health.PROBE])


class PilotTest(unittest.TestCase):
    def test_unavailable_preflight_exits_with_its_code_and_never_probe(self):
        # Docker missing at preflight: the runner's own code, honestly reported, never a probe.
        def boom(*a, **k):
            raise runner.WorkerError("docker is not available", runner.NO_DOCKER)
        printed = []
        with mock.patch.object(runner, "preflight", boom), \
             mock.patch.object(provider_health, "probe") as never, \
             mock.patch("builtins.print", lambda *a, **k: printed.extend(a)):
            code = provider_health.main(["--", "pi"])
        self.assertEqual(code, runner.NO_DOCKER)
        never.assert_not_called()
        self.assertEqual(json.loads(printed[0])["status"], "unavailable")

    def test_a_preflight_failure_names_the_boundary_as_unavailable(self):
        def boom(*a, **k):
            raise runner.WorkerError("the configured worker image is not in the local Docker store",
                                     runner.NO_IMAGE)
        printed = []
        with mock.patch.object(runner, "preflight", boom), \
             mock.patch("builtins.print", lambda *a, **k: printed.extend(a)):
            code = provider_health.main(["--", "pi"])
        self.assertEqual(code, runner.NO_IMAGE)
        got = json.loads(printed[0])
        self.assertEqual(got["status"], "unavailable")
        self.assertEqual(got["events"][0]["status"], "unavailable")

    def test_missing_harness_argv_is_a_usage_error(self):
        with self.assertRaises(SystemExit):
            provider_health.main([])

    def test_a_happy_pilot_prints_ok_and_exits_zero(self):
        printed = []
        def fake_print(text):
            printed.append(text)
        with mock.patch.object(provider_health, "probe") as probe_call, \
             mock.patch.object(runner, "preflight") as preflight_call, \
             mock.patch("builtins.print", fake_print):
            preflight_call.return_value = ({"/tmp/scratch": "/tmp/copy"}, {"KEY": "v"})
            probe_call.return_value = ({"ts": "t", "event": "probe", "tool": "pi", "status": "ok",
                                        "exitCode": 0, "retries": 0, "latencyMs": 812,
                                        "promptBytes": 39, "responseBytes": 2,
                                        "diagnosticBytes": 0}, "ok")
            code = provider_health.main(["--", "pi", "--print"])
        self.assertEqual(code, 0)
        got = json.loads(printed[0])
        self.assertEqual(got["status"], "ok")
        self.assertEqual(got["events"][0]["event"], "preflight")
        self.assertEqual(got["events"][0]["status"], "ok")
        self.assertEqual(got["events"][1]["status"], "ok")
        self.assertEqual(probe_call.call_args.args[1], ["pi", "--print"])
        self.assertFalse(preflight_call.call_args.kwargs["start_docker"])

    def test_probe_id_scopes_the_container_label_without_entering_the_report(self):
        printed = []
        with mock.patch.object(provider_health, "probe", return_value=(
                {"ts": "t", "event": "probe", "tool": "pi", "status": "ok", "exitCode": 0,
                 "retries": 0}, "ok")), \
             mock.patch.object(runner, "preflight", return_value=({}, {})), \
             mock.patch.object(runner, "Sandbox") as create, \
             mock.patch("builtins.print", printed.append):
            provider_health.main(["--probe-id", "a" * 32, "--", "pi"])
        self.assertEqual(create.call_args.args[1], "health-" + "a" * 32)
        self.assertNotIn("a" * 32, printed[0])

    def test_private_scratch_root_contains_and_releases_probe_workdirs(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        captured = []
        def pilot(args, scratch, work):
            captured.extend((scratch, work))
            return 0
        with mock.patch.dict(os.environ, {"FACTORY_HEALTH_SCRATCH_ROOT": root.name}), \
             mock.patch.object(provider_health, "pilot", side_effect=pilot):
            self.assertEqual(provider_health.main(["--", "pi"]), 0)
        self.assertTrue(all(os.path.dirname(path) == root.name for path in captured))
        self.assertTrue(all(not os.path.exists(path) for path in captured))


if __name__ == "__main__":
    unittest.main()
