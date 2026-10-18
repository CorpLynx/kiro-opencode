"""Merge topologies and stitch on-prem devices to their AWS VPN and Direct Connect edges.

Each collector sees one side of the boundary. The AWS collector knows a
customer gateway's public address and a VIF's customer-side address; the
device collectors know which router owns those addresses. Neither can draw
the line between them, so merge does: it unions the pieces by ID (IDs are
deterministic, so the same thing seen twice is one node), then adds `stitch`
links where an on-prem interface address matches an AWS edge. The rules are
in DESIGN.md under "Stitching".

`read_topology` and `find_node` live here because diff and filter need the
same input handling.
"""

from __future__ import annotations

import ipaddress
import sys
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from .model import (AWS_ACCOUNT, AWS_CGW, AWS_DX_CONNECTION, LINK_DX_VIF, LINK_STITCH, NET_DEVICE, Link,
                    Topology, link_id, union)

HELP = "Merge topologies and stitch on-prem devices to their AWS VPN and Direct Connect edges."
PRODUCES_TOPOLOGY = True

RULE_CGW_IP = "cgw-ip"
RULE_DX_VIF = "dx-vif"
RULE_MANUAL = "manual"
RULES = (RULE_CGW_IP, RULE_DX_VIF, RULE_MANUAL)

Pair = Tuple[str, str]
Address = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]
# address -> [(device ID, interface name)]
AddressIndex = Dict[Address, List[Tuple[str, str]]]


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
    ("edge1", "prod (vpc-0abc)"), but labels needn't be unique, so an
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

def merge(topologies: Iterable[Topology], stitch: bool = True,
          manual: Sequence[Union[str, Pair]] = ()) -> Topology:
    """Union `topologies` by ID and stitch on-prem devices to the AWS edge.

    `stitch=False` skips the automatic rules but still applies `manual`
    pairs, since those were asked for by name. A pair is an "A=B" string or
    an (a, b) tuple naming nodes by ID or exact label; an unknown or
    ambiguous name raises ValueError. A summary goes in meta["stitched"].
    """
    topologies = list(topologies)
    out = union(topologies)
    _warn_conflicts(topologies, out)
    _mark_uncollected(out)
    out.meta["stitched"] = add_stitches(out, auto=stitch, manual=manual)
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


# -- stitching -----------------------------------------------------------------

def add_stitches(topo: Topology, auto: bool = True,
                 manual: Sequence[Union[str, Pair]] = ()) -> Dict[str, Any]:
    """Add stitch links to `topo` in place and return the summary merge
    stores in meta["stitched"].

    Manual pairs are resolved first, so a typo fails before anything is
    added. A customer gateway that matches no device is only reported when
    some device has addresses at all: merging AWS pieces alone shouldn't
    warn about every gateway.
    """
    pairs = [_resolve_pair(topo, p) for p in manual]
    links: List[Link] = []
    unmatched: List[str] = []
    if auto:
        index = device_addresses(topo)
        links += _stitch_customer_gateways(topo, index, unmatched)
        links += _stitch_vifs(topo, index)
        named = {end for pair in pairs for end in pair}
        unmatched = [c for c in unmatched if c not in named]
        if index:
            for cgw_id in unmatched:
                cgw = topo.nodes[cgw_id]
                ip = cgw.attrs.get("ip") or "no ip"
                topo.warn(f"customer gateway {cgw.label} [{ip}] matched no device interface:"
                          f" behind NAT? use --stitch DEVICE={cgw_id}")
    stitched = {frozenset((l.source, l.target)) for l in links}
    for a, b in pairs:
        if frozenset((a, b)) not in stitched:
            links.append(_add_stitch(topo, RULE_MANUAL, a, b))
    unique = {l.id: l for l in links}
    counts = {rule: 0 for rule in RULES}
    for link in unique.values():
        counts[link.attrs["rule"]] += 1
    return {"automatic": auto, "counts": counts, "links": sorted(unique),
            "unmatched_customer_gateways": unmatched}


def device_addresses(topo: Topology) -> AddressIndex:
    """Every device interface address, without its prefix length, mapped to
    the devices (and the interface) that hold it."""
    index: AddressIndex = {}
    for device in topo.by_kind(NET_DEVICE):
        for iface in device.attrs.get("interfaces") or []:
            if not isinstance(iface, dict):
                continue
            for text in iface.get("addresses") or []:
                ip = _address(text)
                if ip is None:
                    continue
                owners = index.setdefault(ip, [])
                if all(d != device.id for d, _ in owners):
                    owners.append((device.id, str(iface.get("name") or "")))
    return index


def _stitch_customer_gateways(topo: Topology, index: AddressIndex,
                              unmatched: List[str]) -> List[Link]:
    links: List[Link] = []
    for cgw in topo.by_kind(AWS_CGW):
        ip = _address(cgw.attrs.get("ip"))
        found = _stitch_address(topo, RULE_CGW_IP, index, ip, cgw.id,
                                f"customer gateway {cgw.label}") if ip else []
        if not found:
            unmatched.append(cgw.id)
        links += found
    return links


def _stitch_vifs(topo: Topology, index: AddressIndex) -> List[Link]:
    """A VIF is a link, not a node, so the device is stitched to the DX
    connection the VIF rides on: that is the cable the router plugs into."""
    links: List[Link] = []
    vifs = sorted((l for l in topo.links.values() if l.kind == LINK_DX_VIF), key=lambda l: l.id)
    for vif in vifs:
        connection = _dx_connection(topo, vif)
        ip = _address(vif.attrs.get("customer_address"))
        if connection is None or ip is None:
            continue
        what = f"VIF {vif.attrs.get('vif_id') or vif.id}"
        links += _stitch_address(topo, RULE_DX_VIF, index, ip, connection, what,
                                 vif_id=vif.attrs.get("vif_id"))
    return links


def _dx_connection(topo: Topology, vif: Link) -> Optional[str]:
    for end in (vif.source, vif.target):
        node = topo.nodes.get(end)
        if node is not None and node.kind == AWS_DX_CONNECTION:
            return end
    return None


def _stitch_address(topo: Topology, rule: str, index: AddressIndex, ip: Address, edge: str,
                    what: str, **attrs: Any) -> List[Link]:
    """Stitch every device holding `ip` to `edge`. Several holders usually
    means a lab copy or a stale backup in the inputs; drawing all of them
    and warning beats guessing which one is real."""
    owners = index.get(ip, [])
    if len(owners) > 1:
        topo.warn(f"{ip} ({what}) is on several devices: {', '.join(d for d, _ in owners)};"
                  f" stitched all of them")
    return [_add_stitch(topo, rule, device, edge, ip, port, **attrs) for device, port in owners]


def _add_stitch(topo: Topology, rule: str, device: str, edge: str, ip: Optional[Address] = None,
                port: str = "", **attrs: Any) -> Link:
    """The device's interface becomes its port, so renderers can label that
    end. The ID leaves the port out, so renaming an interface doesn't make
    an unchanged stitch look new to diff."""
    address = str(ip) if ip is not None else ""
    key = address or rule
    return topo.add_link(LINK_STITCH, device, edge, label=address, source_port=port, key=key,
                         id=link_id(LINK_STITCH, device, edge, key=key),
                         rule=rule, address=address or None, **attrs)


def _resolve_pair(topo: Topology, pair: Union[str, Pair]) -> Pair:
    """Resolve a manual pair. A label may itself contain "=", so each split
    point is tried and the first naming two existing nodes wins."""
    if isinstance(pair, str):
        splits = [(pair[:i], pair[i + 1:]) for i, c in enumerate(pair) if c == "="]
        if not splits:
            raise ValueError(f"--stitch {pair!r}: expected NODE=NODE")
        shown = pair
    else:
        splits = [(pair[0], pair[1])]
        shown = f"{pair[0]}={pair[1]}"
    error: Optional[ValueError] = None
    for a, b in splits:
        try:
            ends = find_node(topo, a.strip()), find_node(topo, b.strip())
        except ValueError as e:
            error = error or e
            continue
        if ends[0] == ends[1]:
            raise ValueError(f"--stitch {shown!r}: both ends are {ends[0]}")
        return ends
    raise ValueError(f"--stitch {shown!r}: {error}")


def _address(text: Any) -> Optional[Address]:
    """The address part of "10.0.0.1/30" or "203.0.113.10", or None."""
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        return ipaddress.ip_interface(text.strip()).ip
    except ValueError:
        return None


# -- CLI -----------------------------------------------------------------------

def add_arguments(parser) -> None:
    parser.add_argument("inputs", nargs="+", metavar="TOPOLOGY",
                        help="topology JSON files to merge ('-' reads stdin, once)")
    parser.add_argument("--stitch", action="append", default=[], metavar="NODE=NODE",
                        help="also stitch these two nodes (IDs or exact labels), e.g. a device and"
                             " a customer gateway behind NAT; repeatable")
    parser.add_argument("--no-stitch", action="store_true",
                        help="skip the automatic cgw-ip and dx-vif rules (--stitch pairs still apply)")


def run(args) -> Topology:
    return merge(read_topologies(args.inputs), stitch=not args.no_stitch, manual=args.stitch)
