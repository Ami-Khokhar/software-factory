"""The one sizing metric (factory_mcp/sizing.py): source lines, tests and docs separate."""
import unittest

from factory_mcp import sizing, workitem


class KindTest(unittest.TestCase):
    def test_paths_classify_as_source_tests_or_docs(self):
        cases = {"factory_mcp/foreman.py": "source", "worker/runner.py": "source",
                 "scripts/factory": "source", "pyproject.toml": "source",
                 "tests/test_actions.py": "tests", "tests/fakes.py": "tests",
                 "factory_mcp/test_helper.py": "tests", "a/b/test_thing.py": "tests",
                 "docs/architecture.md": "docs", "README.md": "docs", "docs/run.rst": "docs",
                 "factory_mcp/notes.txt": "source"}
        for path, expected in cases.items():
            with self.subTest(path=path):
                self.assertEqual(sizing.kind(path), expected)


class MeasureTest(unittest.TestCase):
    def test_it_reports_source_tests_and_docs_separately(self):
        files = [("factory_mcp/foreman.py", 500, 159), ("tests/test_actions.py", 300, 84),
                 ("docs/architecture.md", 60, 17)]
        self.assertEqual(sizing.measure(files),
                         {"source": 659, "tests": 384, "docs": 77, "total": 1120, "files": 3})

    def test_binary_numstat_lines_keep_the_file_and_count_zero_lines(self):
        self.assertEqual(sizing.parse_numstat("3\t1\tsrc/a.py\n-\t-\timg/logo.png\n"),
                         [("src/a.py", 3, 1), ("img/logo.png", 0, 0)])

    def test_the_marker_parser_reads_only_its_own_lines(self):
        err = "noise\nFACTORY_PR_NUMSTAT: 5\t0\tsrc/a.py\nFACTORY_PR_SIZE: 1 file changed\n"
        self.assertEqual(sizing.parse_numstat_marker(err), [("src/a.py", 5, 0)])


class SpecTest(unittest.TestCase):
    def test_the_ceiling_defaults_to_the_reviewable_one_when_only_the_target_is_named(self):
        """The finding a391ed41: absent a ceiling, the source spec keeps the documented 1000, not the
        advisory target. A WorkOrder without the source target keeps the older all-lines meaning."""
        self.assertEqual(sizing.source_spec({"maxSourceChangedLines": 700}),
                         {"target": 700, "ceiling": sizing.SOURCE_CEILING, "advisory": True})
        self.assertEqual(sizing.source_spec({"maxSourceChangedLines": 700, "maxSourceChangedLinesCeiling": 900}),
                         {"target": 700, "ceiling": 900, "advisory": True})
        self.assertIsNone(sizing.source_spec({"maxChangedLines": 600}))


class BoundaryTest(unittest.TestCase):
    spec = {"target": 700, "ceiling": 1000, "advisory": True}

    def test_the_accepted_sizes_and_the_recovery_boundary(self):
        cases = ((659, sizing.SIZE_OK), (700, sizing.SIZE_OK), (701, sizing.SIZE_ADVISORY),
                 (1000, sizing.SIZE_ADVISORY), (1001, sizing.SIZE_CEILING))
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(sizing.boundary(source, self.spec), expected)

    def test_a_non_advisory_target_refuses_before_the_ceiling(self):
        hard = {"target": 700, "ceiling": 1000, "advisory": False}
        self.assertEqual(sizing.boundary(701, hard), sizing.SIZE_REFUSED)

    def test_the_ceiling_marks_a_size_only_error(self):
        self.assertTrue(sizing.is_ceiling_error("patch not pushed: source-size-ceiling: 1001 source"))
        self.assertFalse(sizing.is_ceiling_error("changes protected path AGENTS.md"))


class DecompositionTest(unittest.TestCase):
    def test_the_plan_keeps_every_acceptance_criterion_in_order(self):
        order = {"goal": "big", "acceptance": [
            {"claim": "a", "doneWhen": "check a"}, {"claim": "b", "doneWhen": "check b"}]}
        self.assertEqual(workitem.decomposition_plan(order), [
            {"position": 1, "claim": "a", "doneWhen": "check a", "dependsOn": []},
            {"position": 2, "claim": "b", "doneWhen": "check b", "dependsOn": [1]}])

    def test_a_workorder_without_criteria_keeps_one_slice_for_its_goal(self):
        self.assertEqual(workitem.decomposition_plan({"goal": "big", "acceptance": []}),
                         [{"position": 1, "claim": "big", "doneWhen": "the goal's checks pass",
                           "dependsOn": []}])

    def test_the_plan_is_pure(self):
        order = {"goal": "big", "acceptance": [{"claim": "a", "doneWhen": "check a"}]}
        first = workitem.decomposition_plan(order)
        first[0]["claim"] = "mutated"
        self.assertEqual(workitem.decomposition_plan(order)[0]["claim"], "a")


if __name__ == "__main__":
    unittest.main()
