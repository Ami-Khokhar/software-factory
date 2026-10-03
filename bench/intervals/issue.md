# Merge intervals

Add `merge_intervals(intervals)` in `{dir}/intervals.py`, importable as `bench.{run}.intervals`
from the repo root. `bench` and `{dir}` need no `__init__.py`: Python imports them as namespace
packages.

- It takes a list of `(start, end)` pairs of numbers and returns a new list of `(start, end)`
  tuples, sorted by start, in which pairs that overlap or touch (one ends where the next starts)
  are merged into one.
- A pair with `start > end` raises `ValueError`. A pair with `start == end` is valid.
- The list it was given is not changed.
- It runs in O(n log n) time: 200,000 pairs take well under a second.

## Acceptance
- `merge_intervals` follows each rule above — done when: `tests/test_{run}_intervals.py` has a test for each rule, and `python3 -m unittest discover -s tests -p 'test_*.py'` passes
- What must not change: every path outside `bench/` and `tests/test_{run}_intervals.py` — done when: the pull request changes only those paths
