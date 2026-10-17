"""Crawl network devices over SSH using CDP/LLDP, running read-only show commands.

Owned by the live crawler workstream; see DESIGN.md.
"""

from __future__ import annotations

HELP = "Crawl network devices over SSH using CDP/LLDP, running read-only show commands."
PRODUCES_TOPOLOGY = True


def add_arguments(parser) -> None:
    pass


def run(args):
    raise RuntimeError("not implemented yet")
