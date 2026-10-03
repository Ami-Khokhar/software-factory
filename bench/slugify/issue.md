# Slugify titles

Titles become URL slugs. Add `slugify(text, max_length=60)` in `{dir}/slugify.py`, importable as
`bench.{run}.slugify` from the repo root. `bench` and `{dir}` need no `__init__.py`: Python
imports them as namespace packages.

- The slug is runs of lowercase ASCII letters and digits, joined by single hyphens.
- An accented letter becomes its base letter: decompose the text with
  `unicodedata.normalize("NFKD", text)` and drop the combining marks, so "Crème" becomes "creme".
- Every other character separates runs: spaces, punctuation, symbols, and letters with no ASCII
  form, so "foo日本bar" becomes "foo-bar".
- No hyphen starts or ends the slug. A text with no ASCII letter or digit gives "".
- A slug longer than `max_length` is cut at the last hyphen that leaves at most `max_length`
  characters, and that hyphen is dropped, so no run is cut. When the first run alone is longer
  than `max_length`, it is cut to `max_length` characters.
- A `max_length` below 1 raises `ValueError`.

## Acceptance
- `slugify` follows each rule above — done when: `tests/test_{run}_slugify.py` has a test for each rule, and `python3 -m unittest discover -s tests -p 'test_*.py'` passes
- What must not change: every path outside `bench/` and `tests/test_{run}_slugify.py` — done when: the pull request changes only those paths
