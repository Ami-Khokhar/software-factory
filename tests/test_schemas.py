"""The four factory contracts must stay well-formed JSON Schema (draft 2020-12)."""
import json
import unittest
from pathlib import Path

SCHEMAS = sorted((Path(__file__).resolve().parent.parent / "schemas").glob("*.schema.json"))


class SchemaFilesTest(unittest.TestCase):
    def test_four_contracts_present(self):
        names = {p.name for p in SCHEMAS}
        self.assertEqual(names, {
            "workitem.schema.json", "workorder.schema.json",
            "result.schema.json", "verdict.schema.json",
        })

    def test_each_schema_is_valid(self):
        try:
            from jsonschema import Draft202012Validator
        except ImportError:
            self.skipTest("jsonschema not installed")
        for path in SCHEMAS:
            with self.subTest(schema=path.name):
                Draft202012Validator.check_schema(json.loads(path.read_text()))


if __name__ == "__main__":
    unittest.main()
