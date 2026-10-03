# Wrap text

Add `wrap(text, width)` in `{dir}/wrap.py`, importable as `bench.{run}.wrap` from the repo root.
`bench` and `{dir}` need no `__init__.py`: Python imports them as namespace packages. It returns
the lines of `text` wrapped to `width`, as a list of strings.

- Words are the runs of characters that are not whitespace; any whitespace (spaces, tabs, line
  breaks) only separates them.
- Each line holds as many whole words as fit, in order, joined by single spaces. No line is longer
  than `width`.
- A word longer than `width` starts a new line and is split into pieces of `width` characters. Its
  last piece can share a line with the words after it.
- An empty or blank text gives `[]`. A `width` below 1 raises `ValueError`.

## Acceptance
- `wrap` follows each rule above — done when: `tests/test_{run}_wrap.py` has a test for each rule, and `python3 -m unittest discover -s tests -p 'test_*.py'` passes
- What must not change: every path outside `bench/` and `tests/test_{run}_wrap.py` — done when: the pull request changes only those paths
