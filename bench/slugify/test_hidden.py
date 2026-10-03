"""Hidden tests for the slugify task. BENCH_MODULE names the module under test."""
import importlib
import os
import unittest

slugify = importlib.import_module(os.environ["BENCH_MODULE"]).slugify


class SlugifyTest(unittest.TestCase):
    def test_runs_of_lowercase_letters_and_digits_joined_by_single_hyphens(self):
        self.assertEqual(slugify("Hello, World!"), "hello-world")
        self.assertEqual(slugify("ABC 123def"), "abc-123def")
        self.assertEqual(slugify("a  --  b__c"), "a-b-c")

    def test_an_accented_letter_becomes_its_base_letter(self):
        self.assertEqual(slugify("Crème brûlée"), "creme-brulee")
        self.assertEqual(slugify("Ångström"), "angstrom")

    def test_a_letter_with_no_ascii_form_separates_runs(self):
        self.assertEqual(slugify("foo日本bar"), "foo-bar")
        self.assertEqual(slugify("日本語"), "")

    def test_no_hyphen_at_either_end_and_empty_when_nothing_is_left(self):
        self.assertEqual(slugify("  --Title--  "), "title")
        self.assertEqual(slugify(""), "")
        self.assertEqual(slugify("!!! ???"), "")

    def test_a_long_slug_is_cut_at_the_last_hyphen_within_the_limit(self):
        self.assertEqual(slugify("word " * 30, max_length=12), "word-word")
        self.assertEqual(slugify("abcd efgh", max_length=9), "abcd-efgh")
        self.assertEqual(slugify("abcd efgh", max_length=8), "abcd")
        self.assertEqual(slugify("abcd efgh", max_length=4), "abcd")
        self.assertEqual(slugify("x" * 70), "x" * 60)

    def test_a_first_run_longer_than_the_limit_is_cut_to_the_limit(self):
        self.assertEqual(slugify("abcdefgh ij", max_length=3), "abc")

    def test_a_max_length_below_one_raises(self):
        with self.assertRaises(ValueError):
            slugify("title", max_length=0)
