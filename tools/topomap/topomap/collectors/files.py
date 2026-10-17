"""Collect from saved device command output and config backups, without touching devices.

Owned by the parsers workstream; see DESIGN.md.
"""

from __future__ import annotations

HELP = "Collect from saved device command output and config backups, without touching devices."
PRODUCES_TOPOLOGY = True


def add_arguments(parser) -> None:
    pass


def run(args):
    raise RuntimeError("not implemented yet")
