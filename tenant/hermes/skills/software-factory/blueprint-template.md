At most 20000 characters. Name real paths and real function names. A worker copies these names.

## Goal and example
The factory answers a running worker's question, for example a worker asks which acceptance check to run.

## Out of scope
Worker registration stays unchanged.

## Modules
`worker/runner.py`: parse worker ask input through `ask_channel_from(text)`.

## Data shapes
Question: `id: str`, `text: str`, `askedAt: str`.

## Order of work
Read `worker/runner.py` first to reuse the existing ask input format.

## Conventions
Use `python3 -m unittest discover -s tests -p 'test_*.py'` and standard library runtime code.

## Done when
Run `python3 -m unittest discover -s tests -p 'test_*.py'` from the repository root.
