# An LRU cache

Add the class `LRUCache` in `{dir}/lru.py`, importable as `bench.{run}.lru` from the repo root.
`bench` and `{dir}` need no `__init__.py`: Python imports them as namespace packages.

- `LRUCache(capacity)` holds at most `capacity` keys. A `capacity` below 1 raises `ValueError`.
- `get(key, default=None)` returns the value stored for `key`, or `default` when the key is not in
  the cache. A stored value can be `None`.
- `put(key, value)` stores the value. When the key is new and the cache is full, it first evicts
  the least recently used key.
- A `get` that finds its key and every `put` count as a use of that key. A `get` that misses
  changes nothing.
- `len(cache)` is the number of keys in the cache.
- `get` and `put` take O(1) time: 200,000 calls on a cache of 20,000 keys take well under a second.

## Acceptance
- `LRUCache` follows each rule above — done when: `tests/test_{run}_lru.py` has a test for each rule, and `python3 -m unittest discover -s tests -p 'test_*.py'` passes
- What must not change: every path outside `bench/` and `tests/test_{run}_lru.py` — done when: the pull request changes only those paths
