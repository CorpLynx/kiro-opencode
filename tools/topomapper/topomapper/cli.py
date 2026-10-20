"""Command-line interface, shaped like the AWS CLI: `topomapper GROUP VERB`.

    topomapper account collect prod.drawio                     collect and draw in one go
    topomapper account collect network iam prod.json prod.svg  ...more parts, more outputs
    topomapper org collect org.json
    topomapper topology merge org.json prod.json --to all.drawio
    topomapper diagram draw all.json all.svg

Files to write come last, as plain arguments, and each one's extension
says what to write: `.json` is the topology itself, and `.drawio`, `.svg`,
`.mmd` or `.dot` is the diagram, drawn on the spot. With none, the topology
JSON goes to stdout. `topology merge`, which takes any number of inputs,
names its outputs with `--to` instead.

Every verb lives in its own module, which exposes:

    HELP: str                       one-line description for --help
    PRODUCES_TOPOLOGY: bool         True if run() returns a Topology to write out
    add_arguments(parser) -> None   its own arguments, its outputs (dest "output") included
    prepare(args) -> None           optional: sort positional words before anything runs
    run(args) -> Topology | int     a Topology to write, or an exit status

Modules must not import optional dependencies (boto3) at import
time, so `topomapper --help` works with only the standard library.
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

GROUPS = {
    "account": ("Collect from AWS accounts: networks, IAM, EKS, Bedrock, every resource.",
                {"collect": "topomapper.collectors.aws"}),
    "org": ("Collect an AWS Organization: OUs, accounts and policies.",
            {"collect": "topomapper.collectors.aws_org"}),
    "topology": ("Work on saved topologies: merge, filter, compare, anonymize.",
                 {"merge": "topomapper.merge", "filter": "topomapper.filter",
                  "diff": "topomapper.diff", "anonymize": "topomapper.anonymize"}),
    "diagram": ("Draw a saved topology as draw.io, SVG, Mermaid or DOT.",
                {"draw": "topomapper.render"}),
}
OUTPUT_HELP = ("files to write, by extension: .json for the topology, or .drawio, .svg, .mmd "
               "or .dot to draw it (default: topology JSON on stdout)")


def output_path(value: str) -> str:
    """A file to write names a topology (.json, or - for stdout) or a
    diagram; anything else is refused before a long collection starts."""
    ext = os.path.splitext(value)[1].lower()
    if value == "-" or ext == ".json" or ext in DIAGRAMS:
        return value
    raise ValueError(
        f"{value}: say what to write with the extension: .json for the topology, or "
        f"{', '.join(sorted(DIAGRAMS))} for a diagram")


def add_drawing_options(parser: argparse.ArgumentParser) -> None:
    drawing = parser.add_argument_group("drawing (when an output is a diagram)")
    drawing.add_argument("--direction", choices=("TB", "LR"), default="TB",
                         help="top-to-bottom (default) or left-to-right reading order")
    drawing.add_argument("--title", default="", help="title drawn above the diagram")
    drawing.add_argument("--no-link-labels", action="store_true",
                         help="leave ports, subnets and VPN/VIF IDs off the links")


def _add_verb(verbs, name: str, module_path: str) -> None:
    module = importlib.import_module(module_path)
    p = verbs.add_parser(name, description=module.HELP, help=module.HELP)
    module.add_arguments(p)
    if getattr(module, "PRODUCES_TOPOLOGY", False):
        add_drawing_options(p)
    p.set_defaults(_module=module, _parser=p)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="topomapper",
        description="Gather AWS topology automatically, merge it, and draw it. Commands are "
                    "`topomapper GROUP VERB`; any command that makes a topology draws it too "
                    "when an output file is a diagram.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    groups = parser.add_subparsers(dest="group", metavar="group")
    groups.required = True
    for group, (help_text, verbs) in GROUPS.items():
        g = groups.add_parser(group, description=help_text, help=help_text)
        sub = g.add_subparsers(dest="verb", metavar="verb")
        sub.required = True
        for verb, path in verbs.items():
            _add_verb(sub, verb, path)
    return parser


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse, then let the module sort its words and check every output's
    extension, so a typo fails here rather than after the collection."""
    parser = build_parser()
    args, extra = parser.parse_known_args(argv)
    if extra:
        _take_loose_words(args, extra)
    try:
        prepare = getattr(args._module, "prepare", None)
        if prepare:
            prepare(args)
        if getattr(args._module, "PRODUCES_TOPOLOGY", False):
            args.output = list(dict.fromkeys(output_path(p) for p in (args.output or ["-"])))
    except ValueError as e:
        args._parser.error(str(e))
    return args


def _take_loose_words(args: argparse.Namespace, extra: List[str]) -> None:
    """Words after an option (`collect network --region us-east-1 iam`)
    belong to the verb's list of words, which argparse fills only once."""
    unknown = [a for a in extra if a.startswith("-") and a != "-"]
    lists = [a for a in args._parser._actions
             if not a.option_strings and a.nargs in ("*", "+")]
    if unknown or not lists:
        args._parser.error(f"unrecognized arguments: {' '.join(unknown or extra)}")
    dest = lists[-1].dest
    setattr(args, dest, list(getattr(args, dest) or []) + extra)


def write_text(path: str, text: str, private: bool = True) -> None:
    if path == "-":
        sys.stdout.write(text)
    elif private:
        write_private(path, text)
    else:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)


def write_topology(topo: Topology, path: str, private: bool = True) -> None:
    write_text(path, topo.dumps(), private)


def write_outputs(topo: Topology, args: argparse.Namespace, private: bool = True) -> None:
    """Each output: the topology as JSON, or drawn in the format its
    extension names."""
    for path in args.output:
        ext = os.path.splitext(path)[1].lower()
        if path == "-" or ext == ".json":
            write_topology(topo, path, private)
            continue
        from .render import render
        write_text(path, render(topo, DIAGRAMS[ext], direction=args.direction, title=args.title,
                                link_labels=not args.no_link_labels), private)


def _note_untracked(args: argparse.Namespace) -> None:
    """Say so when real data was written where git would pick it up: the
    output files or a redirected stdout."""
    for path in [p if p != "-" else stdout_file() for p in args.output]:
        hint = untracked_hint(path) if path else None
        if hint:
            print(f"topomapper: note: {hint}", file=sys.stderr)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    try:
        result = args._module.run(args)
    except KeyboardInterrupt:
        return 130
    except (OSError, ValueError, RuntimeError) as e:
        # 2, like argparse: 1 is reserved for "differences found" (diff).
        print(f"topomapper: error: {e}", file=sys.stderr)
        return 2
    if isinstance(result, Topology):
        sensitive = getattr(args._module, "SENSITIVE", True)
        try:
            write_outputs(result, args, private=sensitive)
        except (OSError, ValueError) as e:
            print(f"topomapper: error: {e}", file=sys.stderr)
            return 2
        if sensitive:
            _note_untracked(args)
        for warning in result.meta.get("warnings", []):
            print(f"topomapper: warning: {warning}", file=sys.stderr)
        return 0
    return int(result or 0)
