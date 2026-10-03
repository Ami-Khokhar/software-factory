# Parse durations

Add `parse_duration(text)` in `{dir}/duration.py`, importable as `bench.{run}.duration` from the
repo root. `bench` and `{dir}` need no `__init__.py`: Python imports them as namespace packages.
It returns the number of seconds in a duration such as `1h30m`, `45s`, `2d` or `1h 5m 10s`, as
an `int`.

- The units are `d` (86400 seconds), `h` (3600), `m` (60) and `s` (1), in lower case.
- A part is one or more digits followed at once by a unit. Each unit appears at most once, and the
  parts come in the order d, h, m, s. Spaces may stand between parts and around the text.
- Anything else raises `ValueError`, for example: an empty or blank text, a number with no unit, a
  unit with no number, an unknown unit, a unit twice or out of order, a sign, a decimal point.

## Acceptance
- `parse_duration` follows each rule above — done when: `tests/test_{run}_duration.py` has a test for each rule, and `python3 -m unittest discover -s tests -p 'test_*.py'` passes
- What must not change: every path outside `bench/` and `tests/test_{run}_duration.py` — done when: the pull request changes only those paths
