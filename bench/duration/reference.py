import re

DURATION = re.compile(r"\s*(?:(\d+)d)?\s*(?:(\d+)h)?\s*(?:(\d+)m)?\s*(?:(\d+)s)?\s*")
SECONDS = (86400, 3600, 60, 1)


def parse_duration(text):
    match = DURATION.fullmatch(text)
    if not match or not any(match.groups()):
        raise ValueError(f"not a duration: {text!r}")
    return sum(int(part) * unit for part, unit in zip(match.groups(), SECONDS) if part)
