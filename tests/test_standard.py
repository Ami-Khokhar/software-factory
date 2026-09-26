"""The factory standard (ADR-0015) is one block of text. Every factory repo copies it from
templates/AGENTS.md, and this repo's AGENTS.md must carry the same block."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BLOCK = re.compile(r"<!-- factory-standard:begin -->.*?<!-- factory-standard:end -->", re.S)


class StandardTest(unittest.TestCase):
    def test_this_repo_follows_the_standard_it_ships(self):
        template = BLOCK.findall((ROOT / "templates" / "AGENTS.md").read_text())
        ours = BLOCK.findall((ROOT / "AGENTS.md").read_text())
        self.assertEqual((len(template), len(ours)), (1, 1), "each file needs exactly one block")
        self.assertEqual(ours, template)


if __name__ == "__main__":
    unittest.main()
