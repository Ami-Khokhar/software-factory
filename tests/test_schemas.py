"""The schemas are the published contracts, and the code must say what they say.

Every WorkItem and WorkOrder the Foreman writes is validated against its schema in
tests/test_actions.py. Here: each schema is valid JSON Schema, the Result check the Foreman runs on
worker output accepts exactly the Results the schema accepts, and the Verdicts read from gate
comments meet their schema.
"""
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from factory_mcp.documents import check_result
from factory_mcp.verdict import verdicts
from tests.fakes import marker_comment

ROOT = Path(__file__).resolve().parent.parent
ORDER = {"id": "o/r#7:r0:implement:a0", "requiredEvidence": ["tests-pass"]}
PASSING = {"kind": "test", "command": "python3 -m unittest", "exit": 0, "tail": "OK"}
PATCH = {"format": "unified", "diff": "diff --git a/app.py b/app.py"}


def schema(name):
    return json.loads((ROOT / "schemas" / f"{name}.schema.json").read_text())


def result(**fields):
    doc = {"schemaVersion": "factory.result/v1", "workOrderId": ORDER["id"], "status": "completed",
           "patch": PATCH, "evidence": [PASSING],
           "startedAt": "2026-09-24T00:00:00Z", "finishedAt": "2026-09-24T00:10:00Z"}
    doc.update(fields)
    return {key: value for key, value in doc.items() if value is not ...}


BASIC_DIAGNOSTICS = {"elapsedMs": 60000, "phases": [
    {"phase": "environment setup", "elapsedMs": 20000}]}


# (case, Result, accepted). Each case varies one rule the schema states.
CASES = [
    ("legacy: no diagnostics", result(), True),
    ("a unified patch", result(), True),
    ("an mbox patch with its base", result(patch={"format": "mbox", "diff": "From x", "baseSha": "b0"}), True),
    ("completed with no patch", result(patch=...), False),
    ("completed with a null patch", result(patch=None), False),
    ("a pushed branch beside the patch, which is no handoff (ADR-0017)",
     result(artifact={"branch": "factory/7", "commitSha": "c1"}), False),
    ("a patch in an unknown format", result(patch={"format": "zip", "diff": "x"}), False),
    ("a patch with an empty diff", result(patch=dict(PATCH, diff="")), False),
    ("a patch with an unknown field", result(patch=dict(PATCH, stat="1 file")), False),
    ("a patch that is not an object", result(patch="junk"), False),
    ("failed with its error", result(status="failed", patch=..., error="verify exited 1"), True),
    ("failed without an error", result(status="failed", patch=...), False),
    ("timed_out, a relay task status", result(status="timed_out", patch=..., error="slow"), False),
    ("an unknown status", result(status="done"), False),
    ("an unknown field", result(relayTaskId="t1"), False),
    ("another schema version", result(schemaVersion="factory.result/v2"), False),
    ("no finishedAt", result(finishedAt=...), False),
    ("evidence without a command", result(evidence=[{"kind": "test", "exit": 0}]), False),
    ("evidence of an unknown kind", result(evidence=[dict(PASSING, kind="vibes")]), False),
    ("evidence with an unknown field", result(evidence=[dict(PASSING, seconds=3)]), False),
    ("an exit that is a boolean", result(evidence=[dict(PASSING, exit=False)]), False),
    ("an exit of 0.0, which JSON counts as an integer", result(evidence=[dict(PASSING, exit=0.0)]), True),
    ("failed, with evidence that is null",
     result(status="failed", patch=..., error="crashed", evidence=None), False),
    ("a tail that is not text", result(evidence=[dict(PASSING, tail=5)]), False),
    ("an error that is not text", result(status="failed", patch=..., error=7), False),
    ("diagnostics with the overall time only", result(diagnostics={"elapsedMs": 60000}), True),
    ("diagnostics with phases and a tail", result(diagnostics={
        "elapsedMs": 60000,
        "phases": [{"phase": "harness execution", "elapsedMs": 40000, "exitCode": 0, "tail": "OK"}],
        "tail": "OK"}), True),
    ("diagnostics on a failed Result", result(status="failed", patch=..., error="slow",
                                             diagnostics={"elapsedMs": 1}), True),
    ("diagnostics with an unknown phase", result(diagnostics={"elapsedMs": 1, "phases": [
        {"phase": "coffee break", "elapsedMs": 1}]}), False),
    ("diagnostics without elapsedMs", result(diagnostics={"phases": []}), False),
    ("diagnostics with an unknown field", result(diagnostics={"elapsedMs": 1, "host": "w1"}), False),
    ("diagnostics with a text elapsedMs", result(diagnostics={"elapsedMs": "1"}), False),
    ("diagnostics with a negative elapsedMs", result(diagnostics={"elapsedMs": -1}), False),
    ("diagnostics with a boolean exit", result(diagnostics={"elapsedMs": 1, "phases": [
        {"phase": "verification", "elapsedMs": 1, "exitCode": True}]}), False),
    ("diagnostics with an over-long tail", result(diagnostics={"elapsedMs": 1, "tail": "x" * 4001}), False),
    ("diagnostics with a tail at the limit", result(diagnostics={"elapsedMs": 1, "tail": "x" * 4000}), True),
    ("diagnostics with a phase tail that is not text", result(diagnostics={"elapsedMs": 1,
        "phases": [{"phase": "verification", "elapsedMs": 1, "tail": 5}]}), False),
    ("diagnostics that are not an object", result(diagnostics="slow"), False),
    ("a phase exitCode of 0.0, which JSON counts as an integer", result(diagnostics={
        "elapsedMs": 1, "phases": [
            {"phase": "verification", "elapsedMs": 1, "exitCode": 0.0}]}), True),
    ("a phase exitCode of 1.0, a serialised integer", result(diagnostics={
        "elapsedMs": 1, "phases": [
            {"phase": "verification", "elapsedMs": 1, "exitCode": 1.0}]}), True),
]


class SchemaTest(unittest.TestCase):
    def test_each_schema_is_valid_json_schema(self):
        for path in sorted((ROOT / "schemas").glob("*.schema.json")):
            with self.subTest(schema=path.name):
                Draft202012Validator.check_schema(json.loads(path.read_text()))

    def test_check_result_accepts_what_the_result_schema_accepts(self):
        validator = Draft202012Validator(schema("result"))
        for case, doc, accepted in CASES:
            with self.subTest(case):
                self.assertEqual(validator.is_valid(doc), accepted, "the schema")
                self.assertEqual(check_result(doc, ORDER) == [], accepted, check_result(doc, ORDER))

    def test_check_result_holds_a_result_to_its_workorder(self):
        """What the schema cannot know: the WorkOrder's id and its required evidence."""
        self.assertEqual(check_result(result(workOrderId="o/r#7:r0:implement:a1"), ORDER),
                         ["workOrderId is not o/r#7:r0:implement:a0"])
        self.assertEqual(check_result(result(evidence=[dict(PASSING, exit=1)]), ORDER),
                         ["required evidence tests-pass: no passing test entry"])
        self.assertEqual(check_result(None, ORDER), ["the task output has no Result JSON line"])

    def test_a_verdict_read_from_a_gate_comment_meets_its_schema(self):
        findings = [{"severity": "blocking", "path": "app.py", "line": 3, "problem": "x > y", "fix": "swap"},
                    {"severity": "note", "path": None, "line": "7", "problem": "No test"}]
        comments = [{"body": marker_comment(outcome, n, f"s{n}", findings), "created_at": "2026-09-24T02:00:00Z",
                     "user": {"login": "github-actions[bot]"}}
                    for n, outcome in enumerate(("CHANGES", "HUMAN", "MERGE"), start=1)]
        validator = Draft202012Validator(schema("verdict"))
        found = verdicts(comments, "o/r", 5)
        self.assertEqual(len(found), 3)
        for verdict in found:
            self.assertEqual([e.message for e in validator.iter_errors(verdict)], [])
    def test_the_workorder_constraints_migrate_to_advisory_lines(self):
        """ADR-0070's migration: a WorkOrder written before the advisory flag — constraints with no
        maxChangedLinesAdvisory — still validates, and the consumers read it as advisory; the flag
        set true or false validates too."""
        validator = Draft202012Validator(schema("workorder"))
        constraints = [{"maxChangedLines": 600, "maxChangedFiles": 20, "protectedPaths": [".github/"]},
                       {"maxChangedLines": 600, "maxChangedLinesAdvisory": True, "maxChangedFiles": 20,
                        "protectedPaths": [".github/"]},
                       {"maxChangedLines": 600, "maxChangedLinesAdvisory": False, "maxChangedFiles": 20,
                        "protectedPaths": [".github/"]}]
        orders = [dict(ORDER, schemaVersion="factory.workorder/v1", workItemId="o/r#7", round=0,
                       kind="implement", repo="o/r", baseSha="b0", headSha=None, goal="Fix",
                       description="", acceptance=[{"claim": "it works", "doneWhen": "tests pass"}], constraints=c,
                       findingsToAddress=[], lessons=[], requiredEvidence=["tests-pass"],
                       budget={"maxPasses": 2, "wallMs": 1000}, idempotencyKey="k",
                       createdAt="2026-10-13T00:00:00Z") for c in constraints]
        for order in orders:
            with self.subTest(constraints=order["constraints"]):
                self.assertEqual([e.message for e in validator.iter_errors(order)], [])


if __name__ == "__main__":
    unittest.main()
