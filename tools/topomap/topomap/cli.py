"""Command-line interface.

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

COLLECTORS = {
    "aws": "topomap.collectors.aws",
    "aws-org": "topomap.collectors.aws_org",
    "gcp": "topomap.collectors.gcp",
    "gcp-org": "topomap.collectors.gcp_org",
    "files": "topomap.collectors.files",
    "devices": "topomap.collectors.devices",
}
COMMANDS = {
    "merge": "topomap.merge",
    "diff": "topomap.diff",
    "filter": "topomap.filter",
    "render": "topomap.render",
    "anonymize": "topomap.anonymize",
}


def _add_module(subparsers, name: str, module_path: str) -> None:
    module = importlib.import_module(module_path)
    p = subparsers.add_parser(name, help=module.HELP, description=module.HELP)
    module.add_arguments(p)
    if getattr(module, "PRODUCES_TOPOLOGY", False):
        p.add_argument("-o", "--output", default="-",
                       help="where to write the topology JSON (default: stdout)")
    p.set_defaults(_module=module)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="topomap",
        description="Gather network and AWS topology automatically, merge it, and draw it.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="command")
    sub.required = True

    collect = sub.add_parser("collect", help="gather a topology from a source",
                             description="Gather a topology from a source.")
    sources = collect.add_subparsers(dest="source", metavar="source")
    sources.required = True
    for name, path in COLLECTORS.items():
        _add_module(sources, name, path)
    for name, path in COMMANDS.items():
        _add_module(sub, name, path)
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


def _note_untracked(args: argparse.Namespace) -> None:
    """Say so when real data was written where git would pick it up: the
    output file, a redirected stdout, or a `--save-raw` folder."""
    places = [args.output if args.output != "-" else stdout_file()]
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
        write_topology(result, args.output, private=sensitive)
        if sensitive:
            _note_untracked(args)
        for warning in result.meta.get("warnings", []):
            print(f"topomap: warning: {warning}", file=sys.stderr)
        return 0
    return int(result or 0)
