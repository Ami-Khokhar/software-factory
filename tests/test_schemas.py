"""The four factory contracts must stay well-formed JSON Schema (draft 2020-12).

Each contract edit has a fixture under tests/fixtures/ with one instance the schema must accept
and one it must reject.
"""
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCHEMAS = sorted((ROOT / "schemas").glob("*.schema.json"))
FIXTURES = sorted((ROOT / "tests" / "fixtures").glob("edit*.json"))


def validator(name):
    from jsonschema import Draft202012Validator
    schema = json.loads((ROOT / "schemas" / f"{name}.schema.json").read_text())
    return Draft202012Validator(schema)


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

    def test_contract_edit_fixtures(self):
        try:
            import jsonschema  # noqa: F401
        except ImportError:
            self.skipTest("jsonschema not installed")
        self.assertEqual(len(FIXTURES), 6)
        for path in FIXTURES:
            fixture = json.loads(path.read_text())
            check = validator(fixture["schema"])
            with self.subTest(fixture=path.name, instance="valid"):
                self.assertEqual([e.message for e in check.iter_errors(fixture["valid"])], [])
            with self.subTest(fixture=path.name, instance="invalid"):
                self.assertFalse(check.is_valid(fixture["invalid"]))


if __name__ == "__main__":
    unittest.main()
