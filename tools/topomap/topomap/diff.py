"""Show what changed between two topology snapshots (exit status 1 if anything did).

Owned by the merge/diff workstream; see DESIGN.md.
"""

from __future__ import annotations

HELP = "Show what changed between two topology snapshots (exit status 1 if anything did)."
PRODUCES_TOPOLOGY = False


def add_arguments(parser) -> None:
    pass


def run(args):
    raise RuntimeError("not implemented yet")
