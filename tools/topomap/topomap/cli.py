"""Command-line interface.

    topomap aws --profile prod -o aws.drawio            collect and draw in one go
    topomap aws --profile prod -o aws.json -o aws.svg   ...and keep the topology too
    topomap merge org.json aws.json net.json -o all.drawio

Sources (aws, aws-org, files, devices, scan) and the commands that work on
topologies (merge, filter, anonymize) all take `-o`, repeatable, and its
extension says what to write: `.json` (or stdout) is the topology itself,
and `.drawio`, `.svg`, `.mmd` or `.dot` is the diagram, drawn on the spot.
`render` draws a topology saved earlier; `diff` compares two. The older
`topomap collect <source>` still works.

Every subcommand lives in its own module, which exposes:

    HELP: str                       one-line description for --help
    PRODUCES_TOPOLOGY: bool         True if run() returns a Topology to write out
    add_arguments(parser) -> None   its own options (not -o; the CLI adds that)
    run(args) -> Topology | int     a Topology to write, or an exit status

Modules must not import optional dependencies (boto3, netmiko) at import
time, so `topomap --help` works with only the standard library.
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from typing import List, Optional

from . import __version__
from .hygiene import stdout_file, untracked_hint, write_private
from .model import Topology
from .render import EXTENSIONS as DIAGRAMS

COLLECTORS = {
    "aws": "topomap.collectors.aws",
    "aws-org": "topomap.collectors.aws_org",
    "files": "topomap.collectors.files",
    "devices": "topomap.collectors.devices",
    "scan": "topomap.collectors.scan",
}
COMMANDS = {
    "merge": "topomap.merge",
    "diff": "topomap.diff",
    "filter": "topomap.filter",
    "render": "topomap.render",
    "anonymize": "topomap.anonymize",
}


def _output(value: str) -> str:
    """`-o` names a topology (.json, or - for stdout) or a diagram; anything
    else is refused now rather than after a long collection."""
    ext = os.path.splitext(value)[1].lower()
    if value == "-" or ext == ".json" or ext in DIAGRAMS:
        return value
    raise argparse.ArgumentTypeError(
        f"{value}: say what to write with the extension: .json for the topology, or "
        f"{', '.join(sorted(DIAGRAMS))} for a diagram")


def _add_module(subparsers, name: str, module_path: str, **kwargs) -> None:
    module = importlib.import_module(module_path)
    p = subparsers.add_parser(name, description=module.HELP, **{"help": module.HELP, **kwargs})
    module.add_arguments(p)
    if getattr(module, "PRODUCES_TOPOLOGY", False):
        p.add_argument("-o", "--output", action="append", type=_output, metavar="FILE",
                       help="where to write: .json for the topology, or .drawio, .svg, .mmd or "
                            ".dot to draw it; repeatable (default: topology JSON on stdout)")
        drawing = p.add_argument_group("drawing (when -o names a diagram)")
        drawing.add_argument("--direction", choices=("TB", "LR"), default="TB",
                             help="top-to-bottom (default) or left-to-right reading order")
        drawing.add_argument("--title", default="", help="title drawn above the diagram")
        drawing.add_argument("--no-link-labels", action="store_true",
                             help="leave ports, subnets and VPN/VIF IDs off the links")
    p.set_defaults(_module=module)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="topomap",
        description="Gather network, AWS and on-prem topology automatically, merge it, and "
                    "draw it. Any command that makes a topology draws it too: -o x.drawio.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command")
    sub.required = True
    for name, path in COLLECTORS.items():
        _add_module(sub, name, path)
    for name, path in COMMANDS.items():
        _add_module(sub, name, path)

    # The older two-word form, `topomap collect aws`, kept for existing scripts.
    collect = sub.add_parser("collect", description="Same as `topomap <source>`.")  # no help=: unlisted
    sources = collect.add_subparsers(dest="source", metavar="source")
    sources.required = True
    for name, path in COLLECTORS.items():
        _add_module(sources, name, path)
    return parser


def write_topology(topo: Topology, path: str, private: bool = True) -> None:
    text = topo.dumps()
    if path == "-":
        sys.stdout.write(text)
    elif private:
        write_private(path, text)
    else:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)


def write_outputs(topo: Topology, args: argparse.Namespace, private: bool = True) -> None:
    """Each `-o`: the topology as JSON, or drawn in the format its
    extension names."""
    for path in args.output:
        ext = os.path.splitext(path)[1].lower()
        if path == "-" or ext == ".json":
            write_topology(topo, path, private)
            continue
        from .render import render
        text = render(topo, DIAGRAMS[ext], direction=args.direction, title=args.title,
                      link_labels=not args.no_link_labels)
        if private:
            write_private(path, text)
        else:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)


def _note_untracked(args: argparse.Namespace) -> None:
    """Say so when real data was written where git would pick it up: the
    output files, a redirected stdout, or a `--save-raw` folder."""
    places = [p if p != "-" else stdout_file() for p in args.output]
    raw = getattr(args, "save_raw", None)
    if raw:
        places.append(os.path.join(raw, "device.txt"))
    for path in places:
        hint = untracked_hint(path) if path else None
        if hint:
            print(f"topomap: note: {hint}", file=sys.stderr)


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = args._module.run(args)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError) as e:
        # 2, like argparse: 1 is reserved for "differences found" (diff).
        print(f"topomap: error: {e}", file=sys.stderr)
        return 2
    if isinstance(result, Topology):
        sensitive = getattr(args._module, "SENSITIVE", True)
        args.output = list(dict.fromkeys(args.output or ["-"]))
        try:
            write_outputs(result, args, private=sensitive)
        except (OSError, ValueError) as e:
            print(f"topomap: error: {e}", file=sys.stderr)
            return 2
        if sensitive:
            _note_untracked(args)
        for warning in result.meta.get("warnings", []):
            print(f"topomap: warning: {warning}", file=sys.stderr)
        return 0
    return int(result or 0)
