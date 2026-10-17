"""Cut a topology down to the parts you want to draw.

Owned by the merge/diff workstream; see DESIGN.md.
"""

from __future__ import annotations

HELP = "Cut a topology down to the parts you want to draw."
PRODUCES_TOPOLOGY = True


def add_arguments(parser) -> None:
    pass


def run(args):
    raise RuntimeError("not implemented yet")
