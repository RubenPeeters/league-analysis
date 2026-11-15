"""Smoke test.

src/main.py connects to MongoDB and the Riot API when imported, so it cannot be
imported in a test yet; until it is restructured, only its syntax is checked.
"""

import ast
from pathlib import Path

MAIN = Path(__file__).parent.parent / "src" / "main.py"


def test_main_parses():
    ast.parse(MAIN.read_text(encoding="utf-8"))
