"""Draw a topology as draw.io, SVG, Mermaid or Graphviz DOT.

    topomapper diagram draw estate.json estate.drawio estate.svg   # formats from the extensions
    topomapper diagram draw - --format mermaid < estate.json        # stdin to stdout

draw.io and SVG share one layout engine (`layout.py`); Mermaid and DOT
are text that their own tools lay out. All four are deterministic: the
same topology renders to byte-identical output, so rendered diagrams can
be committed next to the snapshots they come from.
"""

from __future__ import annotations

import os
import sys
from typing import Any, Optional

from ..hygiene import stdout_file, untracked_hint, write_private
from ..model import Topology

HELP = "Draw a topology as draw.io, SVG, Mermaid or Graphviz DOT."
PRODUCES_TOPOLOGY = False

FORMATS = ("drawio", "svg", "mermaid", "dot")
EXTENSIONS = {".drawio": "drawio", ".xml": "drawio", ".svg": "svg", ".mmd": "mermaid",
              ".mermaid": "mermaid", ".dot": "dot", ".gv": "dot"}


def add_arguments(parser) -> None:
    parser.add_argument("input", metavar="TOPOLOGY", help="topology JSON file, or - for stdin")
    parser.add_argument("output", nargs="*", metavar="FILE",
                        help="diagrams to write, each in the format its extension names: .drawio, "
                             ".svg, .mmd or .dot (default: stdout)")
    parser.add_argument("--format", choices=FORMATS,
                        help="format for stdout (default: drawio)")
    parser.add_argument("--direction", choices=("TB", "LR"), default="TB",
                        help="top-to-bottom (default) or left-to-right reading order")
    parser.add_argument("--title", default="", help="title drawn above the diagram")
    parser.add_argument("--no-link-labels", action="store_true",
                        help="leave ports, subnets and VPN/VIF IDs off the links")


def prepare(args: Any) -> None:
    """Every output must name a diagram format, refused before anything is read."""
    for path in args.output:
        if os.path.splitext(path)[1].lower() not in EXTENSIONS:
            raise ValueError(f"{path}: name the diagram format with the extension: "
                             f"{', '.join(sorted(EXTENSIONS))}")
    if args.format and args.output:
        raise ValueError("--format is for stdout; with files, their extensions say the format")


def infer_format(path: Optional[str]) -> str:
    """drawio unless the output file's extension says otherwise."""
    if not path or path == "-":
        return "drawio"
    return EXTENSIONS.get(os.path.splitext(path)[1].lower(), "drawio")


def render(topology: Topology, fmt: str = "drawio", *, direction: str = "TB", title: str = "",
           link_labels: bool = True, **unknown: Any) -> str:
    """Render `topology` as `fmt` ("drawio", "svg", "mermaid" or "dot")."""
    if unknown:
        raise TypeError(f"unknown render option(s): {', '.join(sorted(unknown))}")
    if direction not in ("TB", "LR"):
        raise ValueError(f"unknown direction {direction!r} (expected TB or LR)")
    if fmt in ("drawio", "svg"):
        from . import drawio, svg
        from .layout import layout
        lay = layout(topology, direction, title)
        module = drawio if fmt == "drawio" else svg
        return module.render(topology, lay, link_labels=link_labels)
    if fmt == "mermaid":
        from . import mermaid
        return mermaid.render(topology, direction, title, link_labels)
    if fmt == "dot":
        from . import dot
        return dot.render(topology, direction, title, link_labels)
    raise ValueError(f"unknown format {fmt!r} (expected one of {', '.join(FORMATS)})")


def run(args) -> int:
    if args.input == "-":
        topo = Topology.loads(sys.stdin.read())
    else:
        topo = Topology.load(args.input)
    options = dict(direction=args.direction, title=args.title, link_labels=not args.no_link_labels)
    anonymized = bool(topo.meta.get("anonymized"))
    targets = [(p, infer_format(p)) for p in args.output] or [("-", args.format or "drawio")]
    for path, fmt in targets:
        text = render(topo, fmt, **options)
        if path == "-":
            sys.stdout.write(text)
            path = None if anonymized else stdout_file()
        elif anonymized:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            path = None
        else:
            write_private(path, text)
        hint = untracked_hint(path) if path else None
        if hint:
            print(f"topomapper: note: {hint}", file=sys.stderr)
    return 0
