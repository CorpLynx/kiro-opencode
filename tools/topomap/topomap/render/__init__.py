"""Draw a topology as draw.io, SVG, Mermaid or Graphviz DOT.

Owned by the renderers workstream; see DESIGN.md.
"""

from __future__ import annotations

HELP = "Draw a topology as draw.io, SVG, Mermaid or Graphviz DOT."
PRODUCES_TOPOLOGY = False


def add_arguments(parser) -> None:
    pass


def run(args):
    raise RuntimeError("not implemented yet")
