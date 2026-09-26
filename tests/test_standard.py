"""The factory standard (ADR-0015) is one block of text. Every factory repo copies it from
templates/AGENTS.md, and this repo's AGENTS.md must carry the same block."""
import unittest
from pathlib import Path

from factory_mcp.repos import TEMPLATE, standard_blocks

ROOT = Path(__file__).resolve().parent.parent


class StandardTest(unittest.TestCase):
    def test_this_repo_follows_the_standard_it_ships(self):
        template = standard_blocks(TEMPLATE.read_text())
        ours = standard_blocks((ROOT / "AGENTS.md").read_text())
        self.assertEqual((len(template), len(ours)), (1, 1), "each file needs exactly one block")
        self.assertEqual(ours, template)


if __name__ == "__main__":
    unittest.main()
