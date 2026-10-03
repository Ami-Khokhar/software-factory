"""Hidden tests for the intervals task. BENCH_MODULE names the module under test."""
import importlib
import os
import random
import time
import unittest

merge_intervals = importlib.import_module(os.environ["BENCH_MODULE"]).merge_intervals


class MergeIntervalsTest(unittest.TestCase):
    def test_overlapping_pairs_merge_and_the_result_is_sorted(self):
        self.assertEqual(merge_intervals([(15, 18), (1, 3), (8, 10), (2, 6)]), [(1, 6), (8, 10), (15, 18)])
        self.assertEqual(merge_intervals([(1, 10), (2, 3)]), [(1, 10)])

    def test_touching_pairs_merge(self):
        self.assertEqual(merge_intervals([(1, 4), (4, 5)]), [(1, 5)])
        self.assertEqual(merge_intervals([(1.5, 2.5), (2.5, 3)]), [(1.5, 3)])

    def test_empty_and_single_point_pairs(self):
        self.assertEqual(merge_intervals([]), [])
        self.assertEqual(merge_intervals([(2, 2)]), [(2, 2)])
        self.assertEqual(merge_intervals([(5, 6), (1, 2)]), [(1, 2), (5, 6)])

    def test_a_pair_that_ends_before_it_starts_raises(self):
        with self.assertRaises(ValueError):
            merge_intervals([(1, 2), (3, 1)])

    def test_the_given_list_is_not_changed(self):
        given = [(5, 6), (1, 3), (2, 4)]
        merge_intervals(given)
        self.assertEqual(given, [(5, 6), (1, 3), (2, 4)])

    def test_two_hundred_thousand_pairs_take_well_under_a_second(self):
        rng = random.Random(7)
        starts = [rng.randint(0, 10**7) for _ in range(200_000)]
        pairs = [(start, start + rng.randint(0, 5)) for start in starts]
        started = time.perf_counter()
        merged = merge_intervals(pairs)
        self.assertLess(time.perf_counter() - started, 3.0)  # a generous ceiling; a quadratic merge takes minutes
        self.assertTrue(all(a[1] < b[0] for a, b in zip(merged, merged[1:])))
