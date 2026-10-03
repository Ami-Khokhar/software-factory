"""Hidden tests for the lru task. BENCH_MODULE names the module under test."""
import importlib
import os
import random
import time
import unittest

LRUCache = importlib.import_module(os.environ["BENCH_MODULE"]).LRUCache


class LRUCacheTest(unittest.TestCase):
    def test_a_full_cache_evicts_the_least_recently_used_key(self):
        cache = LRUCache(2)
        cache.put("a", 1)
        cache.put("b", 2)
        self.assertEqual(cache.get("a"), 1)  # a is now the most recent
        cache.put("c", 3)
        self.assertEqual((cache.get("b"), cache.get("a"), cache.get("c"), len(cache)), (None, 1, 3, 2))

    def test_a_put_of_a_stored_key_updates_it_and_counts_as_a_use(self):
        cache = LRUCache(2)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.put("a", 10)
        self.assertEqual(len(cache), 2)
        cache.put("c", 3)
        self.assertEqual((cache.get("a"), cache.get("b"), cache.get("c")), (10, None, 3))

    def test_a_miss_returns_the_default_and_changes_nothing(self):
        cache = LRUCache(2)
        cache.put("a", 1)
        cache.put("b", 2)
        self.assertEqual(cache.get("x", "none"), "none")
        self.assertEqual((len(cache), cache.get("a"), cache.get("b")), (2, 1, 2))

    def test_a_stored_none_is_a_hit(self):
        cache = LRUCache(1)
        cache.put("k", None)
        self.assertIsNone(cache.get("k", "default"))

    def test_a_capacity_below_one_raises(self):
        with self.assertRaises(ValueError):
            LRUCache(0)

    def test_calls_take_constant_time(self):
        cache = LRUCache(20_000)
        keys = [("key", i) for i in range(100_000)]
        rng = random.Random(7)
        wanted = [keys[max(0, i - rng.randint(1, 19_999))] for i in range(len(keys))]  # anywhere in the cache
        started = time.perf_counter()
        for i, key in enumerate(keys):
            cache.put(key, i)
            cache.get(wanted[i])
        self.assertLess(time.perf_counter() - started, 3.0)  # a generous ceiling; an O(n) cache takes ~10 s
        self.assertEqual(len(cache), 20_000)
