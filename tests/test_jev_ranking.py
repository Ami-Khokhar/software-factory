"""The opt-in Jev lesson ranking (ADR-0058), against a local fake System One endpoint.

The endpoint is a real HTTP server on 127.0.0.1: the process boundary is the only thing
faked. No test calls TypeSafe, and no test holds a key: the "key" here is any string, and
the server records the Authorization header to prove where it traveled.
"""
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from factory_mcp import jev_rank, lessons
from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.store import Store
from tests.fakes import FakeGitHub, FakeRelay

REPO = "o/r"
KEY = "test-key-not-a-secret"
OLD, NEW = "fp-2026-01-01", "fp-2026-10-01"  # one old lesson, one new: which rides first?
OLD_TEXT = "Every new endpoint carries a test for its 401 path."
NEW_TEXT = "Retire a flag the same week it dies."


class Endpoint(BaseHTTPRequestHandler):
    """A test sets `answer` (a dict for 200, bytes verbatim, an int for that status with a
    JSON error body) and `delay` seconds, then reads `requests` (path, Authorization,
    body)."""

    answer, delay, requests = {}, 0.0, []

    def log_message(self, *args):
        pass

    def handle_error(self, request, client_address):
        pass  # a client that timed out and left: the test asserts the client side, not ours

    def do_POST(self):
        body = self.rfile.read(int(self.headers["content-length"]))
        Endpoint.requests.append({"path": self.path, "auth": self.headers.get("Authorization"),
                                  "body": json.loads(body)})
        if self.delay:
            threading.Event().wait(self.delay)
        reply, status = self.answer, 200
        if isinstance(reply, int):
            status, reply = reply, {"error": "overloaded, and the body repeats the request: " + KEY}
        data = reply if isinstance(reply, bytes) else json.dumps(reply).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def choice(fingerprint, probabilities, confidence=0.9):
    """A valid Choice answer naming `fingerprint`."""
    return {"model": jev_rank.MODEL, "answers": {"relevant": {
        "type": "choice", "choice": fingerprint, "probabilities": probabilities,
        "confidence": confidence}}}


def fallbacks(from_, wi=f"{REPO}#7"):
    """How the ranking fell back on the WorkItem, in order: one fixed category per stop.
    `from_` is a Foreman (reads its store) or a Store direct."""
    store = from_.store if hasattr(from_, "store") else from_
    return [h["detail"]["category"] for h in store.history(wi)
            if h["action"] == "lesson_rank_fallback"]


def valid_two(old_p=0.7, new_p=0.29):
    """A decisive answer for the two-lesson fixture: `old` wins, `new` follows."""
    return choice(OLD, {OLD: old_p, NEW: new_p,
                        jev_rank.ABSTAIN[0]: 1 - old_p - new_p, jev_rank.ABSTAIN[1]: 0.0})


def endpoint(test):
    """A fake endpoint on 127.0.0.1 for `test`, with its requests cleared."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), Endpoint)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    test.addCleanup(server.shutdown)
    test.addCleanup(server.server_close)
    Endpoint.requests = []
    return server


class OnTest(unittest.TestCase):
    """Foreman.dispatch with the ranking on: the packet order a fake answer moves."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = str(Path(self.dir.name) / "lessons.json")
        self.github, self.relay = FakeGitHub(), FakeRelay()
        self.github.add_issue(REPO, 7)
        self.url = f"http://127.0.0.1:{endpoint(self).server_port}"
        self.config = Config(mcp_token="t", repos=frozenset({REPO}), lessons_file=self.path,
                             rank_lessons=True, jev_api_key=KEY, jev_url=self.url)
        self.foreman = Foreman(Store(":memory:"), self.github, self.config, self.relay)
        self.foreman.intake(REPO, 7)

    def approve(self, fingerprint=NEW, text=NEW_TEXT, at="2026-10-01T00:00:00Z"):
        lessons.approve(self.path, REPO, fingerprint, text, "amteshwar",
                        lambda r: [{"fingerprint": fingerprint, "workItems": 2}], at=at)

    def dispatch(self, answer=None, findings=()):
        """One fresh factory run: intake, then the tick that dispatches. A WorkItem dispatches
        once, so each case here starts a new store and proves its own dispatch."""
        Endpoint.answer = valid_two() if answer is None else answer
        self.foreman = Foreman(Store(":memory:"), self.github, self.config, self.relay)
        self.foreman.intake(REPO, 7)
        wi = self.foreman.tick(f"{REPO}#7")["workItem"]
        order = self.foreman.store.workorders(f"{REPO}#7")[-1]
        return wi, order, (Endpoint.requests or [{}])[-1]

    def fallbacks(self):
        return fallbacks(self.foreman)

    def test_an_older_relevant_lesson_ranks_above_a_newer_irrelevant_one(self):
        self.approve(OLD, OLD_TEXT, at="2026-01-01T00:00:00Z")
        self.approve()
        wi, order, _ = self.dispatch()
        self.assertEqual(wi["state"], "implementing")
        self.assertEqual([l["fingerprint"] for l in order["lessons"]], [OLD, NEW])
        self.assertEqual(order["lessons"][0]["text"], OLD_TEXT)

    def test_the_newest_first_order_holds_when_the_answer_ties_or_leaves_gaps(self):
        self.approve(OLD, OLD_TEXT, at="2026-01-01T00:00:00Z")
        self.approve()
        for answer in (choice(OLD, {OLD: 0.55, NEW: 0.44,
                                    jev_rank.ABSTAIN[0]: 0.0, jev_rank.ABSTAIN[1]: 0.01}),
                       choice(OLD, {OLD: 0.6, jev_rank.ABSTAIN[0]: 0.4})):  # NEW has no entry
            wi, order, _ = self.dispatch(answer)
            self.assertEqual([l["fingerprint"] for l in order["lessons"]], [OLD, NEW], answer)

    def test_a_new_lesson_winning_wins(self):
        self.approve(OLD, OLD_TEXT, at="2026-01-01T00:00:00Z")
        self.approve()
        _, order, _ = self.dispatch(choice(NEW, {NEW: 0.8, OLD: 0.19,
                                                 jev_rank.ABSTAIN[0]: 0.01,
                                                 jev_rank.ABSTAIN[1]: 0.0}))
        self.assertEqual([l["fingerprint"] for l in order["lessons"]], [NEW, OLD])

    def test_the_request_carries_only_the_task_and_this_repos_lessons(self):
        self.approve(OLD, OLD_TEXT, at="2026-01-01T00:00:00Z")
        self.approve()
        lessons.approve(self.path, "other/r", "fp-other", "another repo's lesson", "amteshwar",
                        lambda r: [{"fingerprint": "fp-other", "workItems": 2}])
        wi, order, request = self.dispatch()
        question = request["body"]["questions"]["relevant"]
        self.assertEqual(request["body"]["model"], "jev-latest")
        self.assertEqual(question["type"], "choice")
        self.assertEqual(request["body"]["state"], {"query": {
            "goal": wi["goal"], "acceptance": [f"{c['claim']} — done when: {c['doneWhen']}"
                                               for c in wi["acceptance"]]}})
        self.assertEqual(sorted(question["criteria"]), sorted([OLD, NEW] + list(jev_rank.ABSTAIN)))
        sent = json.dumps(request["body"])
        self.assertIn(OLD_TEXT, sent)
        self.assertIn(NEW_TEXT, sent)
        self.assertNotIn("another repo's lesson", sent)
        self.assertNotIn("fp-other", sent)

    def test_the_key_rides_in_the_header_alone(self):
        self.approve()
        self.dispatch()
        self.assertEqual(Endpoint.requests[0]["auth"], f"Bearer {KEY}")
        self.assertNotIn(KEY, json.dumps(Endpoint.requests[0]["body"]))

    def test_every_fallback_keeps_the_newest_first_packet_and_the_line_moving(self):
        self.approve(OLD, OLD_TEXT, at="2026-01-01T00:00:00Z")
        self.approve()
        cases = [  # (answer, expected category): abstain, unknown, malformed, HTTP
            (choice(jev_rank.ABSTAIN[0], {jev_rank.ABSTAIN[0]: 0.9, jev_rank.ABSTAIN[1]: 0.1}),
             "abstain"),
            (choice(jev_rank.ABSTAIN[1], {jev_rank.ABSTAIN[0]: 0.1, jev_rank.ABSTAIN[1]: 0.9}),
             "abstain"),
            (choice("fp-never-sent", {OLD: 0.9}), "invalid-answer"),
            (choice(OLD, {OLD: 0.2, NEW: 0.2, jev_rank.ABSTAIN[0]: 0.3,
                          jev_rank.ABSTAIN[1]: 0.3}), "low-confidence"),
            (choice(OLD, {OLD: 0.9, NEW: "high"}), "bad-response"),  # a probability not a number
            ({"model": jev_rank.MODEL, "answers": {"relevant": {
                "type": "choice", "choice": OLD, "probabilities": {OLD: 0.9}}}},
             "bad-response"),  # the confidence is missing
            ({"model": "jev-latest", "answers": {}}, "bad-response"),
            (b"<html>not json, and it names " + KEY.encode() + b"</html>", "bad-response"),
            (500, "http-500"),
        ]
        for answer, category in cases:
            with self.subTest(category):
                wi, order, _ = self.dispatch(answer)
                self.assertEqual(wi["state"], "implementing")  # no pass spent, no attempt failed
                self.assertEqual([l["fingerprint"] for l in order["lessons"]], [NEW, OLD])
                self.assertEqual(self.fallbacks(), [category])  # this run's one stop, named
                self.assertNotIn(KEY, json.dumps(self.foreman.store.history(f"{REPO}#7")))
                self.assertNotIn(KEY, self.relay.tasks[wi["providerTaskId"]]["input"])

    def test_a_timeout_is_a_fallback_the_line_survives(self):
        self.approve()
        Endpoint.answer, Endpoint.delay = b"{}", 1.0  # the reply lands after the client gave up
        with mock.patch.object(jev_rank, "TIMEOUT_SECONDS", 0.2):
            wi, order, _ = self.dispatch()
        self.assertEqual(wi["state"], "implementing")
        self.assertEqual([l["fingerprint"] for l in order["lessons"]], [NEW])
        self.assertEqual(self.fallbacks(), ["timeout"])

    def test_an_endpoint_that_is_not_there_is_a_fallback(self):
        self.approve()
        self.config = Config(mcp_token="t", repos=frozenset({REPO}), lessons_file=self.path,
                             rank_lessons=True, jev_api_key=KEY, jev_url="http://127.0.0.1:1")
        wi, order, _ = self.dispatch()  # intake again on the fresh foreman ticks the dead host
        self.assertEqual(wi["state"], "implementing")
        self.assertEqual([l["fingerprint"] for l in order["lessons"]], [NEW])
        self.assertEqual(self.fallbacks(), ["unreachable"])

    def test_more_lessons_than_the_option_limit_ranks_nothing_and_cuts_nothing(self):
        for n in range(254):
            lessons.approve(self.path, REPO, f"fp-{n}", f"lesson {n}", "amteshwar",
                            lambda r, n=n: [{"fingerprint": f"fp-{n}", "workItems": 2}],
                            at=f"2026-01-01T00:{n // 60:02d}:{n % 60:02d}Z")
        wi, order, _ = self.dispatch()  # the fake endpoint would rank; too many options wins
        self.assertEqual([l["fingerprint"] for l in order["lessons"]],
                         [f"fp-{n}" for n in range(253, 243, -1)])  # the newest ten, unranked
        self.assertEqual(self.fallbacks(), ["too-many-lessons"])
        self.assertEqual(Endpoint.requests, [])

    def test_an_oversized_request_ranks_nothing(self):
        self.approve(OLD, "x" * 300, at="2026-01-01T00:00:00Z")
        self.approve()
        with mock.patch.object(jev_rank, "MAX_BYTES", 100):
            wi, order, _ = self.dispatch()
        self.assertEqual([l["fingerprint"] for l in order["lessons"]], [NEW, OLD])
        self.assertEqual(self.fallbacks(), ["request-bound"])
        self.assertEqual(Endpoint.requests, [])

    def test_the_cap_applies_after_ranking(self):
        for n in range(12):
            lessons.approve(self.path, REPO, f"fp-{n}", f"lesson {n}", "amteshwar",
                            lambda r, n=n: [{"fingerprint": f"fp-{n}", "workItems": 2}],
                            at=f"2026-01-01T00:{n:02d}:00Z")
        probabilities = {f"fp-{n}": 0.02 for n in range(12)}
        probabilities["fp-0"], probabilities["fp-1"] = 0.5, 0.3  # the two oldest matter most
        _, order, _ = self.dispatch(choice("fp-0", probabilities))
        self.assertEqual(len(order["lessons"]), 10)
        self.assertEqual([l["fingerprint"] for l in order["lessons"]],
                         ["fp-0", "fp-1"] + [f"fp-{n}" for n in range(11, 3, -1)])

    def test_other_repos_lessons_never_ride_along_whatever_the_answer_says(self):
        lessons.approve(self.path, "other/r", "fp-other", "another repo's lesson", "amteshwar",
                        lambda r: [{"fingerprint": "fp-other", "workItems": 2}])
        self.approve()
        wi, order, request = self.dispatch(choice(
            "fp-other", {"fp-other": 0.99, NEW: 0.01}))  # Jev names a lesson it never saw
        self.assertEqual([l["fingerprint"] for l in order["lessons"]], [NEW])
        criteria = request["body"]["questions"]["relevant"]["criteria"]
        self.assertEqual([key for key in criteria if key not in jev_rank.ABSTAIN], [NEW])

    def test_a_fallback_that_echoes_the_key_into_the_result_stays_out_of_the_store(self):
        self.approve()
        wi, order, _ = self.dispatch(503)
        stored = json.dumps(self.foreman.store.history(f"{REPO}#7")) \
            + json.dumps(order) + self.relay.tasks[wi["providerTaskId"]]["input"]
        self.assertNotIn(KEY, stored)
        self.assertEqual(self.fallbacks(), ["http-503"])

    def test_the_current_findings_reach_the_ranker_and_nowhere_else_from(self):
        self.approve()
        wi = self.foreman.store.get(f"{REPO}#7")
        self.foreman.dispatch(wi, "fix", findings=[
            {"fingerprint": "merge-conflict", "problem": "PR #1 conflicts with main."}])
        request = Endpoint.requests[-1]
        self.assertEqual(request["body"]["state"]["query"]["findingsToAddress"],
                         ["PR #1 conflicts with main."])
        self.assertNotIn("human", json.dumps(request["body"]))  # not the audit log's note


class QuietTest(unittest.TestCase):
    """A mode that makes no call, off or broken: the packet stays the newest ten of twelve."""

    rank, key, fallback = False, "", []

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = str(Path(self.dir.name) / "lessons.json")
        self.github, self.relay = FakeGitHub(), FakeRelay()
        self.github.add_issue(REPO, 7)
        self.url = f"http://127.0.0.1:{endpoint(self).server_port}"
        self.foreman = Foreman(Store(":memory:"), self.github,
                               Config(mcp_token="t", repos=frozenset({REPO}),
                                      lessons_file=self.path, rank_lessons=self.rank,
                                      jev_api_key=self.key, jev_url=self.url),
                               self.relay)
        self.foreman.intake(REPO, 7)
        for n in range(12):
            lessons.approve(self.path, REPO, f"fp-{n}", f"lesson {n}", "amteshwar",
                            lambda r, n=n: [{"fingerprint": f"fp-{n}", "workItems": 2}],
                            at=f"2026-01-01T00:{n:02d}:00Z")

    def test_no_call_and_the_same_capped_packet(self):
        self.foreman.tick(f"{REPO}#7")
        order = self.foreman.store.workorders(f"{REPO}#7")[-1]
        self.assertEqual([l["fingerprint"] for l in order["lessons"]],
                         [f"fp-{n}" for n in range(11, 1, -1)])  # the newest ten
        self.assertEqual(Endpoint.requests, [])
        self.assertEqual(fallbacks(self.foreman), self.fallback)


class OffTest(QuietTest):
    """The default: ranking off; not even a fallback row."""


class NoKeyTest(QuietTest):
    """The mode on but the key gone (a restart without its secrets): it says why, once."""

    rank, fallback = True, ["no-key"]


class UnitTest(unittest.TestCase):
    """rank() on the details a Foreman dispatch cannot reach: findings filtering, one call."""

    def setUp(self):
        self.server = endpoint(self)
        self.store = Store(":memory:")
        self.config = Config(mcp_token="t", repos=frozenset({REPO}), rank_lessons=True,
                             jev_api_key=KEY, jev_url=f"http://127.0.0.1:{self.server.server_port}")
        self.entries = [{"fingerprint": OLD, "text": OLD_TEXT}, {"fingerprint": NEW, "text": NEW_TEXT}]
        self.wi = {"id": "u/r#0", "goal": "g", "acceptance": [{"claim": "c", "doneWhen": "d"}]}

    def rank(self, answer=None, findings=()):
        Endpoint.answer = answer if answer is not None else valid_two()
        return jev_rank.rank(self.entries, self.wi, list(findings), self.store, self.config)

    def test_off_makes_no_call(self):
        off = Config(mcp_token="t", repos=frozenset({REPO}),
                     jev_url=f"http://127.0.0.1:{self.server.server_port}")
        self.assertIsNone(jev_rank.rank(self.entries, self.wi, [], self.store, off))
        self.assertEqual(Endpoint.requests, [])

    def test_a_valid_answer_returns_the_same_entries_winner_first(self):
        ranked = self.rank(choice(NEW, {NEW: 0.6, OLD: 0.39,
                                        jev_rank.ABSTAIN[0]: 0.01, jev_rank.ABSTAIN[1]: 0.0}))
        self.assertEqual([e["fingerprint"] for e in ranked], [NEW, OLD])
        self.assertEqual(sorted(map(json.dumps, ranked)), sorted(map(json.dumps, self.entries)))
        self.assertEqual(len(Endpoint.requests), 1)  # one bounded call, and no other

    def test_a_non_dict_answer_is_bad_response(self):
        self.assertIsNone(self.rank({"answers": {"relevant": "choice"}}))
        self.assertEqual(fallbacks(self.store, self.wi["id"]), ["bad-response"])

    def test_findings_with_no_problem_text_are_not_sent(self):
        self.rank(findings=[{"fingerprint": "human"}, {"fingerprint": "x", "problem": "p"}])
        self.assertEqual(Endpoint.requests[-1]["body"]["state"]["query"]["findingsToAddress"], ["p"])


class ConfigTest(unittest.TestCase):
    """The service environment's say: the flag and the key are read once, at start."""

    ENV = {"FACTORY_MCP_TOKEN": "t", "FACTORY_REPOS": REPO}

    def test_ranking_is_off_by_default_and_the_url_is_the_documented_endpoint(self):
        config = Config.from_env(self.ENV)
        self.assertFalse(config.rank_lessons)
        self.assertEqual(config.jev_api_key, "")
        self.assertEqual(config.jev_url, jev_rank.HOST)

    def test_the_flag_turns_ranking_on_and_the_key_comes_from_typesafe_api_key(self):
        config = Config.from_env(dict(self.ENV, FACTORY_JEV_LESSON_RANKING="1",
                                      TYPESAFE_API_KEY=KEY))
        self.assertTrue(config.rank_lessons)
        self.assertEqual(config.jev_api_key, KEY)

    def test_a_flag_without_a_key_refuses_to_start(self):
        with self.assertRaisesRegex(ValueError, "TYPESAFE_API_KEY is not set"):
            Config.from_env(dict(self.ENV, FACTORY_JEV_LESSON_RANKING="true"))


if __name__ == "__main__":
    unittest.main()
