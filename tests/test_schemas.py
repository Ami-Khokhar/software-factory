"""The schemas are the published contracts, and the code must say what they say.

Every WorkItem and WorkOrder the Foreman writes is validated against its schema in
tests/test_actions.py. Here: each schema is valid JSON Schema, and the Result check the Foreman
runs on worker output accepts exactly the Results the schema accepts.
"""
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from factory_mcp.documents import check_result

ROOT = Path(__file__).resolve().parent.parent
ORDER = {"id": "o/r#7:r0:implement:a0", "requiredEvidence": ["tests-pass"]}
PASSING = {"kind": "test", "command": "python3 -m unittest", "exit": 0, "tail": "OK"}


def schema(name):
    return json.loads((ROOT / "schemas" / f"{name}.schema.json").read_text())


def result(**fields):
    doc = {"schemaVersion": "factory.result/v1", "workOrderId": ORDER["id"], "status": "completed",
           "artifact": {"branch": "factory/7", "commitSha": "c1"}, "evidence": [PASSING],
           "startedAt": "2026-09-24T00:00:00Z", "finishedAt": "2026-09-24T00:10:00Z"}
    doc.update(fields)
    return {key: value for key, value in doc.items() if value is not ...}


# (case, Result, accepted). Each case varies one rule the schema states.
CASES = [
    ("a pushed branch", result(), True),
    ("a unified patch", result(artifact=..., patch={"format": "unified", "diff": "diff --git a b"}), True),
    ("an mbox patch with its base", result(artifact=..., patch={"format": "mbox", "diff": "From x",
                                                                "baseSha": "b0"}), True),
    ("completed with no artifact or patch", result(artifact=...), False),
    ("an artifact without a commit", result(artifact={"branch": "factory/7"}), False),
    ("a patch in an unknown format", result(artifact=..., patch={"format": "zip", "diff": "x"}), False),
    ("a patch with an empty diff", result(artifact=..., patch={"format": "unified", "diff": ""}), False),
    ("failed with its error", result(status="failed", artifact=..., error="verify exited 1"), True),
    ("failed without an error", result(status="failed", artifact=...), False),
    ("timed out without an error", result(status="timed_out", artifact=...), False),
    ("cancelled without an error", result(status="cancelled", artifact=...), True),
    ("an unknown status", result(status="done"), False),
    ("an unknown field", result(relayTaskId="t1"), False),
    ("another schema version", result(schemaVersion="factory.result/v2"), False),
    ("no finishedAt", result(finishedAt=...), False),
    ("evidence without a command", result(evidence=[{"kind": "test", "exit": 0}]), False),
    ("evidence of an unknown kind", result(evidence=[dict(PASSING, kind="vibes")]), False),
    ("evidence with an unknown field", result(evidence=[dict(PASSING, seconds=3)]), False),
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


if __name__ == "__main__":
    unittest.main()
