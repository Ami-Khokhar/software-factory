"""The worker's ask channel (ADR-0098): a running pass asks Day Desk a question and gets an answer.

The pass holds one token, the WorkItem holds the question while the worker waits, and
factory_answer_question wakes it. A pass that ended, an unknown token and a second question while
one waits are all refused.
"""
import json
import os
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from factory_mcp import documents, tools
from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.mcp import ToolError
from factory_mcp.server import make_server
from factory_mcp.store import Store
from tests.fakes import FakeGit, FakeGitHub, FakeRelay

REPO, ID = "o/r", "o/r#7"


class AskTest(unittest.TestCase):
    def setUp(self):
        self.github, self.relay = FakeGitHub(), FakeRelay()
        self.git = FakeGit(self.github).install(self)
        self.github.add_issue(REPO, 7)
        self.config = Config(mcp_token="t", repos=frozenset({REPO}), port=0)
        self.store = Store(":memory:")
        self.foreman = Foreman(self.store, self.github, self.config, self.relay)
        self.foreman.intake(REPO, 7)

    def start(self):
        """One dispatched pass: the WorkItem is implementing and its WorkOrder holds the ask channel."""
        self.foreman.tick()
        order = self.store.workorders(ID)[-1]
        self.assertEqual(self.foreman.store.get(ID)["state"], "implementing")
        return order

    def test_each_pass_gets_its_own_ask_channel(self):
        order = self.start()
        self.assertTrue(order["ask"]["url"].startswith("http://host.docker.internal:"))
        self.assertTrue(order["ask"]["url"].endswith("/worker/ask"))
        self.assertGreaterEqual(len(order["ask"]["token"]), 16)
        tokens = {o["ask"]["token"] for o in self.store.workorders(ID)}
        self.assertEqual(len(tokens), 1)  # one pass so far: the token is per pass, not per WorkItem

    def test_a_running_pass_asks_and_day_desk_answers(self):
        order = self.start()
        token = order["ask"]["token"]
        self.assertEqual(self.foreman.worker_ask(token, "Which module owns retries?", wait_seconds=1),
                         {"pending": True})
        question = self.store.get(ID)["question"]
        self.assertEqual(question["text"], "Which module owns retries?")
        self.assertIsNone(question.get("answer"))

        self.foreman.answer_question(ID, question["id"], "The relay client, in relay.py.", "day-desk")
        self.assertEqual(self.foreman.worker_ask(token, "Which module owns retries?", wait_seconds=1),
                         {"answer": "The relay client, in relay.py.", "by": "day-desk"})

    def test_an_answered_question_is_not_answered_twice(self):
        order = self.start()
        self.foreman.worker_ask(order["ask"]["token"], "Which module?", wait_seconds=1)
        question = self.store.get(ID)["question"]
        self.foreman.answer_question(ID, question["id"], "relay.py", "day-desk")
        with self.assertRaisesRegex(ToolError, "already answered"):
            self.foreman.answer_question(ID, question["id"], "something else", "day-desk")

    def test_a_bad_wait_is_refused_before_a_question_is_stored(self):
        order = self.start()
        with self.assertRaisesRegex(ToolError, "not a number"):
            self.foreman.worker_ask(order["ask"]["token"], "Which module?", wait_seconds="soon")
        self.assertIsNone(self.store.get(ID).get("question"))

    def test_a_second_question_while_one_waits_is_refused(self):
        order = self.start()
        self.foreman.worker_ask(order["ask"]["token"], "First?", wait_seconds=1)
        with self.assertRaisesRegex(ToolError, "already waiting"):
            self.foreman.worker_ask(order["ask"]["token"], "Second?", wait_seconds=1)

    def test_an_unknown_token_cannot_ask(self):
        self.start()
        with self.assertRaisesRegex(ToolError, "not running"):
            self.foreman.worker_ask("not-a-token", "Anything?", wait_seconds=1)

    def test_a_finished_pass_cannot_ask_or_be_answered(self):
        order = self.start()
        self.foreman.worker_ask(order["ask"]["token"], "First?", wait_seconds=1)
        question = self.store.get(ID)["question"]
        self.store.put(dict(self.store.get(ID), state="reviewing"))
        with self.assertRaisesRegex(ToolError, "cannot ask|not running"):
            self.foreman.worker_ask(order["ask"]["token"], "Again?", wait_seconds=1)
        with self.assertRaisesRegex(ToolError, "not running a pass"):
            self.foreman.answer_question(ID, question["id"], "Too late", "day-desk")

    def test_status_shows_the_question_only_until_it_is_answered(self):
        order = self.start()
        self.foreman.worker_ask(order["ask"]["token"], "Which module?", wait_seconds=1)
        row = tools.build(self.store, self.foreman)["factory_status"].fn({})["workItems"][0]
        self.assertEqual(row["question"]["text"], "Which module?")
        self.foreman.answer_question(ID, row["question"]["id"], "relay.py", "day-desk")
        row = tools.build(self.store, self.foreman)["factory_status"].fn({})["workItems"][0]
        self.assertNotIn("question", row)

    def test_the_worker_input_carries_the_ask_block_for_the_runner_only(self):
        """The block is what the runner splits off; the prompt before it never holds the token."""
        order = self.start()
        text = documents.worker_input(order, self.store.get(ID)["branch"])
        self.assertIn("factory-ask '<your question>'", text)
        self.assertIn("NO_ANSWER", text)
        prompt, rest = text.split(documents.ASK_CHANNEL)
        block, workorder = rest.split("\n", 1)
        self.assertEqual(json.loads(block), order["ask"])
        self.assertTrue(workorder.startswith("WorkOrder:\n"))
        self.assertNotIn(order["ask"]["token"], prompt)
        self.assertNotIn(order["ask"]["token"], workorder)

    def test_the_worker_command_prints_the_answer(self):
        """The image's factory-ask is the worker's side of the channel: it posts the question, waits,
        and prints exactly the answer, so the harness can read it from its shell."""
        order = self.start()
        server = make_server(self.config, self.store, self.github, self.relay)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        env = {**os.environ, "FACTORY_ASK_URL": f"http://127.0.0.1:{server.server_port}/worker/ask",
               "FACTORY_ASK_TOKEN": order["ask"]["token"], "FACTORY_ASK_WAIT": "2"}
        command = Path(__file__).resolve().parent.parent / "worker" / "factory-ask"
        try:
            process = subprocess.Popen([sys.executable, str(command), "Which module owns retries?"],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                       env=env)
            for _ in range(60):
                time.sleep(0.05)
                if (self.store.get(ID).get("question") or {}).get("text"):
                    break
            question = self.store.get(ID)["question"]
            self.assertEqual(question["text"], "Which module owns retries?")
            self.foreman.answer_question(ID, question["id"], "The relay client, in relay.py.", "day-desk")
            out, err = process.communicate(timeout=10)
        finally:
            server.shutdown()
            server.server_close()
        self.assertEqual(out.strip(), "The relay client, in relay.py.", err)

    def test_the_worker_command_says_no_answer_when_nobody_answers(self):
        env = {**os.environ, "FACTORY_ASK_URL": "http://127.0.0.1:9/worker/ask",
               "FACTORY_ASK_TOKEN": "x" * 32, "FACTORY_ASK_WAIT": "1"}
        command = Path(__file__).resolve().parent.parent / "worker" / "factory-ask"
        done = subprocess.run([sys.executable, str(command), "Anything?"], capture_output=True,
                              text=True, env=env, timeout=10)
        self.assertEqual(done.returncode, 0)
        self.assertTrue(done.stdout.startswith("NO_ANSWER:"), done.stdout)

    def test_the_worker_command_answers_help_itself_without_the_channel(self):
        """`factory-ask --help` is the pass asking how to call the command, not a question for Day
        Desk: it prints the usage, exits 0 and never contacts the channel (ADR-0111)."""
        for flag in ("--help", "-h"):
            with self.subTest(flag=flag):
                env = {**os.environ, "FACTORY_ASK_URL": "http://127.0.0.1:9/worker/ask",
                       "FACTORY_ASK_TOKEN": "x" * 32, "FACTORY_ASK_WAIT": "1"}
                command = Path(__file__).resolve().parent.parent / "worker" / "factory-ask"
                done = subprocess.run([sys.executable, str(command), flag], capture_output=True,
                                      text=True, env=env, timeout=10)
                self.assertEqual(done.returncode, 0, done.stderr)
                self.assertTrue(done.stdout.startswith("factory-ask: give a question"), done.stdout)
                self.assertNotIn("NO_ANSWER", done.stdout + done.stderr)

    def test_a_question_starting_with_a_dash_is_not_usage_but_reaches_the_channel(self):
        """Only an argv of exactly -h or --help is usage; a real question may begin with a dash
        (for example '-1 is the retry count...'), and it goes to the channel like any question."""
        env = {**os.environ, "FACTORY_ASK_URL": "http://127.0.0.1:9/worker/ask",
               "FACTORY_ASK_TOKEN": "x" * 32, "FACTORY_ASK_WAIT": "1"}
        command = Path(__file__).resolve().parent.parent / "worker" / "factory-ask"
        done = subprocess.run([sys.executable, str(command),
                               "-1 is the retry count in the log, which module owns it?"],
                              capture_output=True, text=True, env=env, timeout=10)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(done.stdout.startswith("NO_ANSWER:"), done.stdout)
        self.assertNotIn("factory-ask: give a question", done.stdout + done.stderr)

    def test_the_http_route_carries_the_question_and_the_answer(self):
        order = self.start()
        server = make_server(self.config, self.store, self.github, self.relay)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_port}/worker/ask"

        def post(payload):
            request = urllib.request.Request(url, json.dumps(payload).encode(),
                                             {"content-type": "application/json"})
            with urllib.request.urlopen(request, timeout=10) as response:
                return json.loads(response.read())

        try:
            self.assertEqual(post({"token": order["ask"]["token"], "question": "Which module?",
                                   "wait": 1}), {"pending": True})
            question = self.store.get(ID)["question"]
            self.foreman.answer_question(ID, question["id"], "relay.py", "day-desk")
            self.assertEqual(post({"token": order["ask"]["token"], "question": "Which module?",
                                   "wait": 1}), {"answer": "relay.py", "by": "day-desk"})
            with self.assertRaises(urllib.error.HTTPError) as refused:
                post({"token": "not-a-token", "question": "Hi", "wait": 1})
            self.assertEqual(refused.exception.code, 409)
            with self.assertRaises(urllib.error.HTTPError) as non_ascii:
                post({"token": "tökén", "question": "Hi", "wait": 1})
            self.assertEqual(non_ascii.exception.code, 409)
            with self.assertRaises(urllib.error.HTTPError) as bad_wait:
                post({"token": order["ask"]["token"], "question": "Hi", "wait": "soon"})
            self.assertEqual(bad_wait.exception.code, 409)
        finally:
            server.shutdown()
            server.server_close()
