"""Hidden tests for the wrap task. BENCH_MODULE names the module under test."""
import importlib
import os
import unittest

wrap = importlib.import_module(os.environ["BENCH_MODULE"]).wrap


class WrapTest(unittest.TestCase):
    def test_lines_hold_as_many_whole_words_as_fit(self):
        self.assertEqual(wrap("the quick brown fox", 10), ["the quick", "brown fox"])
        self.assertEqual(wrap("abc def", 7), ["abc def"])
        self.assertEqual(wrap("abc def", 6), ["abc", "def"])
        self.assertEqual(wrap("a b c", 1), ["a", "b", "c"])

    def test_any_whitespace_only_separates_words(self):
        self.assertEqual(wrap("one\ntwo\t\tthree   four", 30), ["one two three four"])

    def test_a_long_word_starts_a_new_line_in_pieces_of_width(self):
        self.assertEqual(wrap("abcdefghij", 4), ["abcd", "efgh", "ij"])
        self.assertEqual(wrap("abcdefgh", 4), ["abcd", "efgh"])
        self.assertEqual(wrap("a bcdefgh", 4), ["a", "bcde", "fgh"])

    def test_the_last_piece_of_a_long_word_shares_a_line_with_what_follows(self):
        self.assertEqual(wrap("abcdefghij x", 4), ["abcd", "efgh", "ij x"])

    def test_blank_text_gives_no_lines(self):
        self.assertEqual(wrap("", 5), [])
        self.assertEqual(wrap(" \n\t ", 5), [])

    def test_a_width_below_one_raises(self):
        with self.assertRaises(ValueError):
            wrap("text", 0)
