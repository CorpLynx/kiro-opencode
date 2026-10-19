"""Cut a topology down to the parts you want to draw.

A full merge of a real estate is too much for one diagram. Filtering has
three parts, applied in this order:

1. **Select.** `--kind`, `--match`, `--around` and `--aws-only` /
   `--network-only` each pick nodes from the whole input, and a node stays
   only if every option given picks it. Measuring each against the whole
   input keeps the result independent of option order.
2. **Keep context.** Ancestors of selected nodes stay, so a subnet is still
   drawn inside its VPC. `--exclude-kind` then removes nodes of a kind;
   children of a removed container move up to the nearest kept ancestor,
   which is how `--exclude-kind az` gives a flatter VPC.
3. **Collapse.** `--collapse vpc` folds everything inside each VPC into it,
   re-pointing links that ended inside at the VPC. `--max-per-type N` folds
   the resources `topomap aws --all-resources` found, where one box holds
   more than N of a type, into one "286 × certificate" node.

Links survive only if both ends do, so the result always passes
`Topology.validate()`.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .merge import find_node, read_topology
from .model import AWS_RESOURCE, NODE_KINDS, Link, Node, Topology, link_id

HELP = "Cut a topology down to the parts you want to draw."
PRODUCES_TOPOLOGY = True

NAMESPACES = {"aws": "aws:", "network": "net:"}
EXTERNAL_PREFIX = "ext:"
PORT_ATTRS = ("source_port", "target_port")


def filter_topology(topo: Topology, kinds: Sequence[str] = (), exclude_kinds: Sequence[str] = (),
                    collapse: Sequence[str] = (), around: Optional[str] = None, depth: int = 1,
                    match: Optional[str] = None, aws_only: bool = False,
                    network_only: bool = False, hide_empty: bool = False,
                    max_per_type: Optional[int] = None) -> Topology:
    """Return a filtered copy of `topo` (the input is not changed).

    `around` is a node ID or exact label; its neighbourhood is everything
    within `depth` link hops of it and, for a container, of its contents.
    `match` is a regular expression searched in each node's label and ID.
    `hide_empty` drops regions and OUs the collectors flagged `empty`, which
    is most of a many-region diagram. What was applied is appended to
    meta["filters"].
    """
    for option, values in (("kind", kinds), ("exclude-kind", exclude_kinds),
                           ("collapse", collapse)):
        _check_kinds(option, values)
    if aws_only and network_only:
        raise ValueError("--aws-only and --network-only exclude each other")
    if depth < 0:
        raise ValueError(f"--depth must be 0 or more, not {depth}")
    if max_per_type is not None and max_per_type < 1:
        raise ValueError(f"--max-per-type must be 1 or more, not {max_per_type}")

    applied: Dict[str, Any] = {}
    picks: List[Set[str]] = []
    if aws_only or network_only:
        applied["only"] = "aws" if aws_only else "network"
        picks.append(_in_namespace(topo, NAMESPACES[applied["only"]]))
    if kinds:
        applied["kind"] = sorted(set(kinds))
        picks.append({n.id for n in topo.nodes.values() if n.kind in kinds})
    if match is not None:
        applied["match"] = match
        picks.append(_matching(topo, match))
    if around is not None:
        root = find_node(topo, around)
        applied.update(around=root, depth=depth)
        picks.append(_neighbourhood(topo, root, depth))

    if exclude_kinds:
        applied["exclude_kind"] = sorted(set(exclude_kinds))
    excluded = {n.id for n in topo.nodes.values() if n.kind in exclude_kinds}
    if hide_empty:
        applied["hide_empty"] = True
        excluded |= {n.id for n in topo.nodes.values()
                     if n.attrs.get("empty") is True and not topo.children(n.id)}
    selected = set(topo.nodes) - excluded
    for pick in picks:
        selected &= pick
    keep = {i for s in selected for i in [s, *topo.ancestors(s)] if i in topo.nodes} - excluded

    out = _subgraph(topo, keep)
    if collapse:
        applied["collapse"] = sorted(set(collapse))
        _collapse(out, collapse)
    if max_per_type is not None:
        applied["max_per_type"] = max_per_type
        _summarize(out, max_per_type)
    if applied:
        out.meta["filters"] = list(out.meta.get("filters") or []) + [applied]
    if topo.nodes and not out.nodes:
        out.warn("filter kept no nodes")
    return out


# -- selecting -----------------------------------------------------------------

def _check_kinds(option: str, kinds: Iterable[str]) -> None:
    """A misspelt kind would otherwise silently select nothing."""
    unknown = sorted(set(kinds) - NODE_KINDS)
    if unknown:
        raise ValueError(f"--{option}: unknown kind {', '.join(unknown)}"
                         f" (known: {', '.join(sorted(NODE_KINDS))})")


def _in_namespace(topo: Topology, prefix: str) -> Set[str]:
    """Nodes whose ID is under `prefix`, plus `ext:` nodes linked to one of
    them: the internet beside an internet gateway is context, not another
    estate. Stitch links cross namespaces, so they drop out on their own."""
    inside = {i for i in topo.nodes if i.startswith(prefix)}
    external = set()
    for link in topo.links.values():
        for a, b in ((link.source, link.target), (link.target, link.source)):
            if a in inside and b.startswith(EXTERNAL_PREFIX) and b in topo.nodes:
                external.add(b)
    return inside | external


def _matching(topo: Topology, pattern: str) -> Set[str]:
    try:
        rx = re.compile(pattern)
    except re.error as e:
        raise ValueError(f"--match {pattern!r}: {e}") from None
    return {n.id for n in topo.nodes.values() if rx.search(n.id) or rx.search(n.label)}


def _neighbourhood(topo: Topology, root: str, depth: int) -> Set[str]:
    """Nodes within `depth` link hops of `root`. A container root starts
    from its contents too: the neighbourhood of a VPC or a site means what
    is in it and what that connects to, and containers rarely have links of
    their own."""
    adjacent: Dict[str, Set[str]] = {}
    for link in topo.links.values():
        adjacent.setdefault(link.source, set()).add(link.target)
        adjacent.setdefault(link.target, set()).add(link.source)
    seen = {root} | _descendants(topo, root)
    frontier = set(seen)
    for _ in range(depth):
        frontier = {m for n in frontier for m in adjacent.get(n, ()) if m not in seen}
        if not frontier:
            break
        seen |= frontier
    return seen


def _descendants(topo: Topology, root: str) -> Set[str]:
    children: Dict[Optional[str], List[str]] = {}
    for node in topo.nodes.values():
        children.setdefault(node.parent, []).append(node.id)
    found: Set[str] = set()
    stack = list(children.get(root, []))
    while stack:
        node_id = stack.pop()
        if node_id not in found and node_id != root:
            found.add(node_id)
            stack.extend(children.get(node_id, []))
    return found


def _subgraph(topo: Topology, keep: Set[str]) -> Topology:
    """Copy of `topo` with only `keep`. Each node's parent becomes its
    nearest kept ancestor, so removing a container lifts its children."""
    out = Topology(copy.deepcopy(topo.meta))
    for node_id in sorted(keep):
        node = topo.nodes[node_id]
        parent = next((a for a in topo.ancestors(node_id) if a in keep), None)
        out.nodes[node_id] = Node(node.id, node.kind, node.label, parent, copy.deepcopy(node.attrs))
    for link in topo.links.values():
        if link.source in keep and link.target in keep:
            out.links[link.id] = Link(link.id, link.kind, link.source, link.target, link.label,
                                      link.directed, copy.deepcopy(link.attrs))
    return out


# -- collapsing ----------------------------------------------------------------

def _collapse(topo: Topology, kinds: Sequence[str]) -> None:
    """Fold everything below each node of `kinds` into it, in place.

    The outermost node wins when collapsible kinds nest (`--collapse region
    --collapse vpc` folds VPCs into their region). The collapsed node gets
    `collapsed: {kind: count}` so a renderer can say what it hides.
    """
    folded: Dict[str, str] = {}
    depth = {n.id: len(topo.ancestors(n.id)) for n in topo.nodes.values()}
    targets = sorted((n for n in topo.nodes.values() if n.kind in kinds),
                     key=lambda n: (depth[n.id], n.id))
    for node in targets:
        if node.id in folded:
            continue
        counts: Dict[str, int] = {}
        for inner in _descendants(topo, node.id):
            folded[inner] = node.id
            kind = topo.nodes[inner].kind
            counts[kind] = counts.get(kind, 0) + 1
        if counts:
            node.attrs["collapsed"] = dict(sorted(counts.items()))
    for inner in folded:
        del topo.nodes[inner]
    _repoint_links(topo, folded)


def _summarize(topo: Topology, limit: int) -> None:
    """Where one box holds more than `limit` resources of one type, draw
    them as a single node that counts them, in place. Only leaves fold: a
    resource holding others is a container and stays."""
    from .render.labels import short_type
    groups: Dict[Tuple[str, str], List[Node]] = {}
    for node in topo.by_kind(AWS_RESOURCE):
        if node.parent and not topo.children(node.id):
            key = (node.parent, str(node.attrs.get("resource_type") or node.attrs.get("service")))
            groups.setdefault(key, []).append(node)
    folded: Dict[str, str] = {}
    for (parent, rtype), members in sorted(groups.items()):
        if len(members) <= limit:
            continue
        first = members[0].attrs
        sid = f"aws:resource:{parent}/{rtype}"
        for member in members:
            folded[member.id] = sid
            del topo.nodes[member.id]
        topo.add_node(sid, AWS_RESOURCE, f"{len(members)} × {short_type(rtype) or rtype}", parent,
                      service=first.get("service"), resource_type=rtype, region=first.get("region"),
                      summarized=len(members))
    _repoint_links(topo, folded)


def _repoint_links(topo: Topology, folded: Dict[str, str]) -> None:
    """Move link ends that pointed inside a collapsed node onto it.

    Links that end up joining the same two nodes with the same kind become
    one (`collapsed_links` counts them when there were several), and one
    that already joined them unchanged absorbs them. Self-loops go. Only
    attributes all merged links agree on are kept, and a port only on an
    end that didn't move, since the inner device it named is gone.
    """
    unchanged: Dict[str, Link] = {}
    groups: Dict[Tuple[str, str, str, bool], List[Tuple[Link, List[Tuple[str, Any]]]]] = {}
    for link in sorted(topo.links.values(), key=lambda l: l.id):
        source, target = folded.get(link.source, link.source), folded.get(link.target, link.target)
        if (source, target) == (link.source, link.target):
            unchanged[link.id] = link
            continue
        if source == target:
            continue
        ends = [(source, link.attrs.get("source_port") if source == link.source else None),
                (target, link.attrs.get("target_port") if target == link.target else None)]
        if not link.directed:
            ends.sort(key=lambda e: e[0])
        groups.setdefault((link.kind, ends[0][0], ends[1][0], link.directed), []).append((link, ends))

    existing = {(l.kind, l.source, l.target, l.directed) for l in unchanged.values()}
    topo.links = dict(unchanged)
    for (kind, source, target, directed), members in groups.items():
        lid = link_id(kind, source, target, directed=directed)
        if (kind, source, target, directed) in existing or lid in topo.links:
            continue
        links = [link for link, _ in members]
        attrs = _common_attrs([{k: v for k, v in l.attrs.items() if k not in PORT_ATTRS}
                               for l in links])
        for i, port_attr in enumerate(PORT_ATTRS):
            ports = {ends[i][1] for _, ends in members}
            if len(ports) == 1 and None not in ports:
                attrs[port_attr] = ports.pop()
        if len(links) > 1:
            attrs["collapsed_links"] = len(links)
        labels = {l.label for l in links}
        topo.links[lid] = Link(lid, kind, source, target, labels.pop() if len(labels) == 1 else "",
                               directed, attrs)


def _common_attrs(attr_sets: List[Dict[str, Any]]) -> Dict[str, Any]:
    first, rest = attr_sets[0], attr_sets[1:]
    return {k: copy.deepcopy(v) for k, v in first.items() if all(a.get(k) == v for a in rest)}


# -- CLI -----------------------------------------------------------------------

def add_arguments(parser) -> None:
    parser.add_argument("input", nargs="?", default="-", metavar="TOPOLOGY",
                        help="topology JSON to filter (default: stdin)")
    parser.add_argument("--kind", action="append", default=[], metavar="KIND",
                        help="keep only nodes of this kind, plus their ancestors; repeatable")
    parser.add_argument("--exclude-kind", action="append", default=[], metavar="KIND",
                        help="remove nodes of this kind; a removed container's children move up"
                             " to its parent; repeatable")
    parser.add_argument("--collapse", action="append", default=[], metavar="KIND",
                        help="fold everything inside each node of this kind into it, re-pointing"
                             " links (e.g. --collapse vpc); repeatable")
    parser.add_argument("--max-per-type", type=int, metavar="N",
                        help="where a box holds more than N resources of one type (from collect "
                             "aws --all-resources), draw them as one node that counts them")
    parser.add_argument("--around", metavar="NODE",
                        help="keep only what is within --depth link hops of this node (ID or"
                             " exact label) or of its contents")
    parser.add_argument("--depth", type=int, metavar="N",
                        help="link hops for --around (default: 1)")
    parser.add_argument("--match", metavar="REGEX",
                        help="keep only nodes whose label or ID matches; prefix (?i) to ignore case")
    parser.add_argument("--hide-empty", action="store_true",
                        help="remove regions and OUs flagged empty (nothing found in them)")
    only = parser.add_mutually_exclusive_group()
    only.add_argument("--aws-only", action="store_true",
                      help="keep only AWS nodes (IDs under aws:) and external nodes linked to them")
    only.add_argument("--network-only", action="store_true",
                      help="keep only network nodes (IDs under net:) and external nodes linked to them")


def run(args) -> Topology:
    if args.depth is not None and args.around is None:
        raise ValueError("--depth needs --around")
    return filter_topology(read_topology(args.input), kinds=args.kind,
                           exclude_kinds=args.exclude_kind, collapse=args.collapse,
                           around=args.around, depth=1 if args.depth is None else args.depth,
                           match=args.match, aws_only=args.aws_only,
                           network_only=args.network_only, hide_empty=args.hide_empty,
                           max_per_type=args.max_per_type)
