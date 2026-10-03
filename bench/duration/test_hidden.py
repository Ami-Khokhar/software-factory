"""Hidden tests for the duration task. BENCH_MODULE names the module under test."""
import importlib
import os
import unittest

parse_duration = importlib.import_module(os.environ["BENCH_MODULE"]).parse_duration


class ParseDurationTest(unittest.TestCase):
    def test_each_unit_and_their_sum(self):
        self.assertEqual(parse_duration("45s"), 45)
        self.assertEqual(parse_duration("90m"), 5400)
        self.assertEqual(parse_duration("1h30m"), 5400)
        self.assertEqual(parse_duration("2d"), 172800)
        self.assertEqual(parse_duration("1d2h3m4s"), 93784)
        self.assertEqual(parse_duration("0s"), 0)

    def test_the_result_is_an_int(self):
        self.assertIs(type(parse_duration("1h")), int)

    def test_spaces_between_parts_and_around_the_text(self):
        self.assertEqual(parse_duration("1h 5m 10s"), 3910)
        self.assertEqual(parse_duration("  3m  "), 180)

    def test_anything_else_raises(self):
        for text in ("", "   ", "5", "h", "1x", "1m1h", "1h1h", "-1s", "+1s", "1.5h", "1H", "1h30"):
            with self.subTest(text=text), self.assertRaises(ValueError):
                parse_duration(text)
