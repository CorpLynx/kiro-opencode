"""What each node *is* for drawing purposes, shared by every renderer.

Kinds alone are too coarse to draw with: a `device` may be a router, a
core switch or a phone, and a subnet is public or private. `classify`
turns each node into a visual class once, so the layout engine (which
needs icon sizes and a reading order) and every renderer (which need a
shape) agree on what they are drawing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional

from ..model import (AWS_ACCOUNT, AWS_AZ, AWS_BEDROCK, AWS_IAM, AWS_ORG, AWS_ORG_POLICY, AWS_ORG_ROOT, AWS_OU,
                     AWS_REGION, AWS_SUBNET, AWS_VPC, LINK_DX_VIF, LINK_POLICY, LINK_TRUST,
                     LINK_DXGW_ASSOCIATION, LINK_L3, LINK_PHYSICAL, LINK_SEGMENT, LINK_STITCH,
                     LINK_TGW_ATTACHMENT, LINK_TGW_PEERING, LINK_VGW_ATTACHMENT, LINK_VPC_PEERING,
                     LINK_VPN, NET_DEVICE, NET_EXTERNAL, NET_SEGMENT, NET_SITE, Link, Topology)

CONTAINER_KINDS = frozenset({AWS_ORG, AWS_ORG_ROOT, AWS_OU, AWS_ACCOUNT, AWS_REGION, AWS_VPC,
                             AWS_AZ, AWS_SUBNET, AWS_BEDROCK, AWS_IAM, NET_SITE})

AWS_LEAF_KINDS = frozenset({
    "internet-gateway", "egress-only-internet-gateway", "nat-gateway", "vpc-endpoint",
    "transit-gateway", "vpn-gateway", "customer-gateway", "dx-connection", "dx-gateway", "instance",
    "eks-cluster", "bedrock-model", "iam-role", "iam-user",
})


@dataclass(frozen=True)
class Shape:
    """Icon geometry. `boxed` shapes carry their label inside a rectangle;
    the others are icons with the label underneath."""
    w: int
    h: int
    tier: int
    boxed: bool = False


# Tier is the reading order, top to bottom: the outside world, then the
# edge (gateways, firewalls, routers), then core, access, and endpoints.
SHAPES: Dict[str, Shape] = {
    "external": Shape(60, 36, 0),
    "org-policy": Shape(140, 36, 1, boxed=True),
    "account-management": Shape(48, 48, 2),
    "account-leaf": Shape(48, 48, 3),
    "customer-gateway": Shape(48, 48, 1),
    "dx-connection": Shape(48, 48, 1),
    "internet-gateway": Shape(48, 48, 2),
    "egress-only-internet-gateway": Shape(48, 48, 2),
    "vpn-gateway": Shape(48, 48, 2),
    "transit-gateway": Shape(48, 48, 2),
    "dx-gateway": Shape(48, 48, 2),
    "firewall": Shape(64, 50, 2),
    "router": Shape(50, 50, 2),
    "load-balancer": Shape(64, 50, 3),
    "segment": Shape(120, 36, 3, boxed=True),
    "switch-core": Shape(50, 50, 3),
    "switch-access": Shape(50, 50, 4),
    "wireless-controller": Shape(64, 50, 4),
    "device": Shape(100, 40, 4, boxed=True),
    "generic": Shape(100, 40, 4, boxed=True),
    "nat-gateway": Shape(48, 48, 4),
    "vpc-endpoint": Shape(48, 48, 4),
    "instance": Shape(48, 48, 5),
    "eks-cluster": Shape(48, 48, 5),
    "bedrock-model": Shape(130, 36, 4, boxed=True),
    "iam-role": Shape(48, 48, 4),
    "iam-user": Shape(40, 40, 4),
    "server": Shape(28, 50, 5),
    "phone": Shape(58, 50, 5),
    "wireless-ap": Shape(50, 50, 5),
}
CONTAINER_TIER = 3

CONTAINER_CLASSES = ("organization", "org-root", "ou", "account", "region", "vpc", "az", "bedrock", "iam",
                     "subnet-public", "subnet-private", "site", "group")

# Colours shared by the SVG renderer (draw.io uses its own built-in styles).
CONTAINER_COLORS: Dict[str, tuple] = {
    # class: (stroke, fill, dashed, title colour)
    "organization": ("#232F3E", "none", False, "#232F3E"),
    "org-root": ("#E7157B", "none", True, "#E7157B"),
    "ou": ("#E7157B", "none", False, "#B0084D"),
    "account": ("#CD2264", "none", False, "#CD2264"),
    "region": ("#00A4A6", "none", True, "#147EBA"),
    "vpc": ("#8C4FFF", "none", False, "#6B3FD4"),
    "az": ("#147EBA", "none", True, "#147EBA"),
    "bedrock": ("#01A88D", "#F0FAF8", False, "#017A66"),
    "iam": ("#DD344C", "#FEF5F6", False, "#B02A3D"),
    "subnet-public": ("#7AA116", "#F2F6E8", False, "#248814"),
    "subnet-private": ("#00A4A6", "#E6F6F7", False, "#147EBA"),
    "site": ("#7D8998", "none", False, "#5A6C86"),
    "group": ("#5A6C86", "none", True, "#5A6C86"),
}


@dataclass(frozen=True)
class LinkLook:
    color: str
    width: float
    dash: str = ""       # SVG stroke-dasharray, "" for solid


LINK_LOOKS: Dict[str, LinkLook] = {
    LINK_PHYSICAL: LinkLook("#333333", 1.5),
    LINK_L3: LinkLook("#3C78D8", 1.5, "6 4"),
    LINK_SEGMENT: LinkLook("#6C8EBF", 1.5),
    LINK_TGW_ATTACHMENT: LinkLook("#8C4FFF", 2),
    LINK_TGW_PEERING: LinkLook("#8C4FFF", 2, "8 4"),
    LINK_VPC_PEERING: LinkLook("#8C4FFF", 1.5, "8 4"),
    LINK_VGW_ATTACHMENT: LinkLook("#8C4FFF", 1.5),
    LINK_VPN: LinkLook("#C98B00", 2, "6 4"),
    LINK_DX_VIF: LinkLook("#1A73E8", 4),
    LINK_DXGW_ASSOCIATION: LinkLook("#1A73E8", 2),
    LINK_STITCH: LinkLook("#E7157B", 2, "2 4"),
    LINK_POLICY: LinkLook("#E7157B", 1, "4 3"),
    LINK_TRUST: LinkLook("#DD344C", 1.5, "6 3"),
}
DEFAULT_LINK_LOOK = LinkLook("#666666", 1.5)


def link_look(link: Link) -> LinkLook:
    return LINK_LOOKS.get(link.kind, DEFAULT_LINK_LOOK)


_CORE_RE = re.compile(r"core|dist|agg|spine|backbone")
_ACCESS_RE = re.compile(r"acc|leaf|idf|floor|edge")
_WLC_RE = re.compile(r"wlc|controller|9800|5520|3504|8540")
_DEVICE_ROLES = {"router": "router", "firewall": "firewall", "load-balancer": "load-balancer",
                 "server": "server", "phone": "phone"}


def effective_parents(topo: Topology) -> Dict[str, Optional[str]]:
    """Each node's parent, ignoring parents that don't exist and breaking
    parent cycles (at the smallest ID), so drawing never loops or loses a
    node over a malformed input."""
    parent = {nid: (n.parent if n.parent in topo.nodes and n.parent != nid else None)
              for nid, n in topo.nodes.items()}
    for nid in sorted(parent):
        path: List[str] = []
        on_path = set()
        cur: Optional[str] = nid
        while cur is not None and cur not in on_path:
            on_path.add(cur)
            path.append(cur)
            cur = parent[cur]
        if cur is not None:
            parent[min(path[path.index(cur):])] = None
    return parent


def classify(topo: Topology, parents: Dict[str, Optional[str]]) -> Dict[str, str]:
    """Visual class of every node (see SHAPES and CONTAINER_CLASSES)."""
    has_kids = {p for p in parents.values() if p is not None}
    neighbours: Dict[str, List[str]] = {}
    for link in sorted(topo.links.values(), key=lambda l: l.id):
        if link.source in topo.nodes and link.target in topo.nodes:
            neighbours.setdefault(link.source, []).append(link.target)
            neighbours.setdefault(link.target, []).append(link.source)

    out: Dict[str, str] = {}
    for nid in sorted(topo.nodes):
        out[nid] = _base_class(topo, nid, nid in has_kids)
    for nid in sorted(topo.nodes):
        if out[nid] == "switch":
            out[nid] = _switch_class(topo, nid, neighbours.get(nid, []), out)
    return out


def _base_class(topo: Topology, nid: str, has_kids: bool) -> str:
    node = topo.nodes[nid]
    kind = node.kind
    if kind == AWS_SUBNET:
        return "subnet-public" if node.attrs.get("public") is True else "subnet-private"
    if kind == AWS_ACCOUNT and not has_kids:
        # An account in an organization diagram, with nothing collected inside it.
        return "account-management" if node.attrs.get("management") else "account-leaf"
    if kind == AWS_ORG_POLICY:
        return "org-policy"
    if kind in CONTAINER_KINDS:
        return kind
    if has_kids:
        return "group"
    if kind in AWS_LEAF_KINDS:
        return kind
    if kind == NET_SEGMENT:
        return "segment"
    if kind == NET_EXTERNAL:
        return "external"
    if kind == NET_DEVICE:
        role = str(node.attrs.get("role") or "unknown")
        if role in _DEVICE_ROLES:
            return _DEVICE_ROLES[role]
        if role == "switch":
            return "switch"
        if role == "wireless":
            text = " ".join(str(node.attrs.get(k) or "") for k in ("model", "platform"))
            return ("wireless-controller" if _WLC_RE.search(f"{node.label} {text}".lower())
                    else "wireless-ap")
        return "device"
    return "generic"


def _switch_class(topo: Topology, nid: str, nbrs: List[str], classes: Dict[str, str]) -> str:
    """Core or access. Names usually say; otherwise a switch facing routers,
    firewalls or several other switches is treated as core."""
    name = topo.nodes[nid].label.lower()
    if _CORE_RE.search(name):
        return "switch-core"
    if _ACCESS_RE.search(name):
        return "switch-access"
    upstream = {"router", "firewall", "external", "load-balancer", "segment"}
    if any(classes.get(n) in upstream for n in nbrs):
        return "switch-core"
    if sum(1 for n in set(nbrs) if classes.get(n) in ("switch", "switch-core", "switch-access")) >= 3:
        return "switch-core"
    return "switch-access"


def is_container_class(vclass: str) -> bool:
    return vclass in CONTAINER_CLASSES


def tier(vclass: str) -> int:
    if vclass in SHAPES:
        return SHAPES[vclass].tier
    return CONTAINER_TIER


def shape(vclass: str) -> Shape:
    return SHAPES.get(vclass, SHAPES["generic"])
