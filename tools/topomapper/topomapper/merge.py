"""Merge topologies into one.

Each collection sees part of the estate: an organization chart, one
account's networks, another account collected separately. Merging unions them by ID (IDs are
deterministic, so the same thing seen twice is one node), which is how
accounts land inside their OUs.

`read_topology` and `find_node` live here because diff and filter need the
same input handling.
"""

from __future__ import annotations

import sys
from typing import Iterable, List, Sequence

from .model import AWS_ACCOUNT, Topology, union

HELP = "Merge topologies into one: accounts land in their OUs, and repeated things become one."
PRODUCES_TOPOLOGY = True


# -- input ---------------------------------------------------------------------

def read_topology(path: str) -> Topology:
    """Load a topology from a file, or from stdin for "-". Errors name the
    path, because merge and diff take several inputs and "missing key 'id'"
    alone doesn't say which one is broken."""
    try:
        if path == "-":
            return Topology.loads(sys.stdin.read())
        return Topology.load(path)
    except ValueError as e:
        raise ValueError(f"{path}: {e}") from None
    except (KeyError, TypeError, AttributeError) as e:
        raise ValueError(f"{path}: not a valid topology ({type(e).__name__}: {e})") from None


def read_topologies(paths: Sequence[str]) -> List[Topology]:
    if list(paths).count("-") > 1:
        raise ValueError("stdin ('-') can only be read once")
    return [read_topology(p) for p in paths]


def find_node(topo: Topology, ref: str) -> str:
    """Resolve a node given by ID or by exact label. People think in labels
    ("prod (vpc-0abc)"), but labels needn't be unique, so an
    ambiguous label is an error that lists the IDs to choose from."""
    if ref in topo.nodes:
        return ref
    matches = sorted(n.id for n in topo.nodes.values() if n.label == ref)
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise ValueError(f"label {ref!r} names several nodes, use an ID: {', '.join(matches)}")
    raise ValueError(f"no node with ID or label {ref!r}")


# -- merge ---------------------------------------------------------------------

def merge(topologies: Iterable[Topology]) -> Topology:
    """Union `topologies` by ID, warning where inputs disagree."""
    topologies = list(topologies)
    out = union(topologies)
    _warn_conflicts(topologies, out)
    _mark_uncollected(out)
    for problem in out.validate():
        out.warn(f"merged topology: {problem}")
    return out


def _mark_uncollected(topo: Topology) -> None:
    """When some accounts were collected, an account that only the
    organization listed says so, instead of looking like an empty one."""
    accounts = topo.by_kind(AWS_ACCOUNT)
    if any(a.attrs.get("collected") for a in accounts):
        for account in accounts:
            account.attrs.setdefault("collected", False)


def _warn_conflicts(topologies: List[Topology], merged: Topology) -> None:
    """union() keeps the first kind and parent it sees for an ID. Inputs that
    disagree usually mean two collectors built one ID for different things,
    which deserves a warning rather than a silently odd diagram."""
    for topo in topologies:
        for n in topo.nodes.values():
            kept = merged.nodes[n.id]
            if n.kind != kept.kind:
                merged.warn(f"node {n.id} is a {kept.kind} in one input and a {n.kind} in another;"
                            f" kept {kept.kind}")
            if n.parent is not None and n.parent != kept.parent:
                merged.warn(f"node {n.id} is inside {kept.parent} in one input and {n.parent} in"
                            f" another; kept {kept.parent}")


# -- CLI -----------------------------------------------------------------------

def add_arguments(parser) -> None:
    parser.add_argument("inputs", nargs="+", metavar="TOPOLOGY",
                        help="topology JSON files to merge ('-' reads stdin, once)")
    parser.add_argument("--to", dest="output", action="extend", nargs="+", metavar="FILE",
                        help="the files to write, last on the line, by extension: .json for the "
                             "topology, or .drawio, .svg, .mmd or .dot to draw it (default: "
                             "topology JSON on stdout)")


def run(args) -> Topology:
    return merge(read_topologies(args.inputs))
