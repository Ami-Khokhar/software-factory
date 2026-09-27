"""The Freebuff PTY harness drives the first-run picker and waits for its durable log."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "worker"))
import freebuff  # noqa: E402


FAKE_FREEBUFF = r'''#!/usr/bin/env python3
import json, os, pathlib, sys, time, tty
tty.setraw(0)
mode = json.loads((pathlib.Path(os.environ["HOME"]) / ".config/manicode/settings.json").read_text()).get("fakeMode")
print("Start coding for free", flush=True)
print("GLM 5.3 Flash", flush=True)
if mode == "crash":
    sys.exit(7)
sys.stdin.buffer.read(1)
print("Enter a coding task or / for commands", flush=True)
paste = b""
while b"\x1b[201~" not in paste:
    paste += os.read(0, 4096)
end = paste.index(b"\x1b[201~")
pathlib.Path("received-prompt").write_bytes(paste[len(b"\x1b[200~"):end])
root = pathlib.Path(os.environ["HOME"]) / ".config/manicode/projects" / pathlib.Path.cwd().name / "chats/session"
root.mkdir(parents=True)
log = root / "log.jsonl"
log.write_text(json.dumps({"msg": "[send-message] Sending message with sdk run config"}) + "\n")
if mode == "never":
    time.sleep(30)
time.sleep(0.03)
with log.open("a") as stream:
    stream.write(json.dumps({"msg": "Main prompt finished"}) + "\n")
while os.read(0, 1) not in (b"\r", b"\n"):
    pass
'''


class FreebuffHarnessTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.source_home = root / "source-home"
        config = self.source_home / ".config" / "manicode"
        config.mkdir(parents=True)
        (config / "credentials.json").write_text('{"token":"secret"}')
        (config / "settings.json").write_text('{"freebuffModel":"z-ai/glm-5.3-flash"}')
        self.command = root / "freebuff"
        self.command.write_text(FAKE_FREEBUFF)
        self.command.chmod(0o755)
        self.work = root / "work"
        self.work.mkdir()

    def test_runs_default_glm_prompt_and_waits_for_the_session_completion(self):
        with mock.patch.object(freebuff, "FREEBUFF", str(self.command)), \
                mock.patch.object(freebuff, "WORKDIR", str(self.work)):
            freebuff.run("private task text\nwith a second line", timeout=2, source_home=str(self.source_home))
        self.assertEqual((self.work / "received-prompt").read_text(), "private task text\nwith a second line")

    def test_empty_prompt_fails_without_starting_freebuff(self):
        with self.assertRaisesRegex(freebuff.HarnessError, "prompt must not be empty"):
            freebuff.run("  \n", timeout=2, source_home=str(self.source_home))

    def test_terminal_control_in_workorder_is_rejected_before_start(self):
        with mock.patch.object(freebuff, "FreebuffPty") as start:
            with self.assertRaisesRegex(freebuff.HarnessError, "terminal control"):
                freebuff.run("issue text\x1b[201~\r/exit", timeout=2,
                             source_home=str(self.source_home))
        start.assert_not_called()

    def test_child_crash_and_missing_completion_fail_the_run(self):
        with mock.patch.object(freebuff, "FREEBUFF", str(self.command)), \
                mock.patch.object(freebuff, "WORKDIR", str(self.work)):
            (self.source_home / ".config/manicode/settings.json").write_text('{"fakeMode":"crash"}')
            with self.assertRaisesRegex(freebuff.HarnessError, "exited (before|while accepting input)"):
                freebuff.run("task", timeout=1, source_home=str(self.source_home))
            (self.source_home / ".config/manicode/settings.json").write_text('{"fakeMode":"never"}')
            with self.assertRaisesRegex(freebuff.HarnessError, "did not record prompt completion"):
                freebuff.run("task", timeout=0.5, source_home=str(self.source_home))

    def test_repo_agents_are_declined_and_unknown_access_prompts_fail(self):
        pty = object.__new__(freebuff.FreebuffPty)
        pty.output = ""
        pty.declined_repo_prompt = False
        pty.write = mock.Mock()
        pty._check_prompts("Load and run these? [y/N]")
        pty.write.assert_called_once_with("n\r")
        with self.assertRaisesRegex(freebuff.HarnessError, "authentication, payment, or permission"):
            pty._check_prompts("Authentication required")

    def test_tool_output_after_composer_does_not_trigger_login_guard(self):
        pty = object.__new__(freebuff.FreebuffPty)
        pty.output = ""
        pty.declined_repo_prompt = False
        pty.check_prompts = False
        pty.write = mock.Mock()
        pty._check_prompts("test output: Permission denied")
        pty.write.assert_not_called()

    def test_large_multiline_prompt_is_written_without_truncation(self):
        prompt = "first line\n" + "x" * 20_000 + "\nlast line"
        with mock.patch.object(freebuff, "FREEBUFF", str(self.command)), \
                mock.patch.object(freebuff, "WORKDIR", str(self.work)):
            freebuff.run(prompt, timeout=4, source_home=str(self.source_home))
        self.assertEqual((self.work / "received-prompt").read_text(), prompt)

    def test_completion_before_sdk_start_does_not_finish_the_task(self):
        home = Path(self.tmp.name) / "home"
        log = home / ".config/manicode/projects/work/chats/s/log.jsonl"
        log.parent.mkdir(parents=True)
        log.write_text("\n".join((
            json.dumps({"msg": "Main prompt finished"}),
            json.dumps({"msg": "[send-message] Sending message with sdk run config"}),
        )) + "\n")
        self.assertFalse(freebuff.log_finished(log))

    def test_copies_credentials_without_copying_old_project_logs(self):
        old_log = self.source_home / ".config/manicode/projects/work/chats/old/log.jsonl"
        old_log.parent.mkdir(parents=True)
        old_log.write_text('{}\n')
        home = Path(freebuff.isolated_home(str(self.source_home)))
        self.addCleanup(lambda: freebuff.shutil.rmtree(home, ignore_errors=True))
        self.assertTrue((home / ".config/manicode/credentials.json").is_file())
        self.assertFalse((home / ".config/manicode/projects").exists())

    def test_missing_credentials_fail_before_starting_freebuff(self):
        empty_home = Path(self.tmp.name) / "empty-home"
        with self.assertRaisesRegex(freebuff.HarnessError, "no Freebuff credentials"):
            freebuff.isolated_home(str(empty_home))


if __name__ == "__main__":
    unittest.main()
