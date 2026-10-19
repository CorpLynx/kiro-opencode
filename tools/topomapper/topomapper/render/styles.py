"""What each node *is* for drawing purposes, shared by every renderer.

Kinds alone are too coarse to draw with: a subnet is public or private,
and an account is a container or, in an organization chart, a leaf. `classify`
turns each node into a visual class once, so the layout engine (which
needs icon sizes and a reading order) and every renderer (which need a
shape) agree on what they are drawing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from ..model import (AWS_ACCOUNT, AWS_AZ, AWS_BEDROCK, AWS_IAM, AWS_ORG, AWS_ORG_POLICY, AWS_ORG_ROOT, AWS_OU,
                     AWS_REGION, AWS_SERVICE, AWS_SUBNET, AWS_VPC, LINK_DX_VIF, LINK_POLICY,
                     LINK_RELATED, LINK_TRUST, LINK_DXGW_ASSOCIATION,
                     LINK_TGW_ATTACHMENT, LINK_TGW_PEERING, LINK_VGW_ATTACHMENT, LINK_VPC_PEERING,
                     LINK_VPN, NET_EXTERNAL, Link, Topology)

CONTAINER_KINDS = frozenset({AWS_ORG, AWS_ORG_ROOT, AWS_OU, AWS_ACCOUNT, AWS_REGION, AWS_VPC,
                             AWS_AZ, AWS_SUBNET, AWS_BEDROCK, AWS_IAM, AWS_SERVICE})

AWS_LEAF_KINDS = frozenset({
    "internet-gateway", "egress-only-internet-gateway", "nat-gateway", "vpc-endpoint",
    "transit-gateway", "vpn-gateway", "customer-gateway", "dx-connection", "dx-gateway", "instance",
    "eks-cluster", "bedrock-model", "iam-role", "iam-user", "aws-resource",
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
# edge (customer gateways, Direct Connect), then gateways, then what sits
# inside the VPCs.
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
    "generic": Shape(100, 40, 4, boxed=True),
    "nat-gateway": Shape(48, 48, 4),
    "vpc-endpoint": Shape(48, 48, 4),
    "instance": Shape(48, 48, 5),
    "eks-cluster": Shape(48, 48, 5),
    "aws-resource": Shape(48, 48, 4),
    "bedrock-model": Shape(130, 36, 4, boxed=True),
    "iam-role": Shape(48, 48, 4),
    "iam-user": Shape(40, 40, 4),
}
CONTAINER_TIER = 3

CONTAINER_CLASSES = ("organization", "org-root", "ou", "account", "region", "vpc", "az", "bedrock", "iam",
                     "subnet-public", "subnet-private", "group", "aws-service")

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
    "group": ("#5A6C86", "none", True, "#5A6C86"),
    "aws-service": ("#7D8998", "none", True, "#232F3E"),
}


@dataclass(frozen=True)
class LinkLook:
    color: str
    width: float
    dash: str = ""       # SVG stroke-dasharray, "" for solid


LINK_LOOKS: Dict[str, LinkLook] = {
    LINK_TGW_ATTACHMENT: LinkLook("#8C4FFF", 2),
    LINK_TGW_PEERING: LinkLook("#8C4FFF", 2, "8 4"),
    LINK_VPC_PEERING: LinkLook("#8C4FFF", 1.5, "8 4"),
    LINK_VGW_ATTACHMENT: LinkLook("#8C4FFF", 1.5),
    LINK_VPN: LinkLook("#C98B00", 2, "6 4"),
    LINK_DX_VIF: LinkLook("#1A73E8", 4),
    LINK_DXGW_ASSOCIATION: LinkLook("#1A73E8", 2),
    LINK_POLICY: LinkLook("#E7157B", 1, "4 3"),
    LINK_TRUST: LinkLook("#DD344C", 1.5, "6 3"),
    LINK_RELATED: LinkLook("#879196", 1, "3 3"),
}
DEFAULT_LINK_LOOK = LinkLook("#666666", 1.5)


def link_look(link: Link) -> LinkLook:
    return LINK_LOOKS.get(link.kind, DEFAULT_LINK_LOOK)


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
    return {nid: _base_class(topo, nid, nid in has_kids) for nid in sorted(topo.nodes)}


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
    if kind == NET_EXTERNAL:
        return "external"
    return "generic"


def is_container_class(vclass: str) -> bool:
    return vclass in CONTAINER_CLASSES


def tier(vclass: str) -> int:
    if vclass in SHAPES:
        return SHAPES[vclass].tier
    return CONTAINER_TIER


def shape(vclass: str) -> Shape:
    return SHAPES.get(vclass, SHAPES["generic"])
