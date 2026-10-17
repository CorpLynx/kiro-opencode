"""Merge topologies and stitch on-prem devices to their AWS VPN and Direct Connect edges.

Owned by the merge/diff workstream; see DESIGN.md.
"""

from __future__ import annotations

HELP = "Merge topologies and stitch on-prem devices to their AWS VPN and Direct Connect edges."
PRODUCES_TOPOLOGY = True


def add_arguments(parser) -> None:
    pass


def run(args):
    raise RuntimeError("not implemented yet")
