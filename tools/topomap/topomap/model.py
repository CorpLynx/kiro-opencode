"""The topology graph every collector produces and every renderer consumes.

A topology is a set of nodes and links:

* A **node** is anything drawn as a box or icon: a device, a VPC, a subnet,
  a transit gateway. Nodes nest through `parent` (account > region > VPC >
  AZ > subnet, or site > device). Renderers draw parents as containers.
* A **link** is a line between two nodes: a cable seen by CDP/LLDP, a
  shared subnet, a TGW attachment, a VPN.

IDs are deterministic strings built from what identifies the thing
("aws:vpc:vpc-0abc", "net:device:core1"), so collecting twice, or from two
sources, yields the same IDs and merging is a plain union. The JSON form is
sorted, so snapshots diff cleanly in git.

Kinds and the attributes each kind carries are documented in DESIGN.md.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Optional

SCHEMA = "topomap/1"

# -- node kinds --------------------------------------------------------------

AWS_ACCOUNT = "account"
AWS_REGION = "region"
AWS_VPC = "vpc"
AWS_AZ = "az"
AWS_SUBNET = "subnet"
AWS_IGW = "internet-gateway"
AWS_EIGW = "egress-only-internet-gateway"
AWS_NAT = "nat-gateway"
AWS_ENDPOINT = "vpc-endpoint"
AWS_TGW = "transit-gateway"
AWS_VGW = "vpn-gateway"
AWS_CGW = "customer-gateway"
AWS_DX_CONNECTION = "dx-connection"
AWS_DX_GATEWAY = "dx-gateway"
AWS_INSTANCE = "instance"
AWS_ORG = "organization"
AWS_ORG_ROOT = "org-root"
AWS_OU = "ou"
AWS_ORG_POLICY = "org-policy"
AWS_BEDROCK = "bedrock"                # the Bedrock models one account has in one region
AWS_BEDROCK_MODEL = "bedrock-model"
AWS_IAM = "iam"                        # an account's IAM roles and users, drawn as one box
AWS_IAM_ROLE = "iam-role"
AWS_IAM_USER = "iam-user"
AWS_EKS = "eks-cluster"

GCP_ORG = "gcp-organization"
GCP_FOLDER = "gcp-folder"
GCP_PROJECT = "gcp-project"
GCP_NETWORK = "gcp-network"            # a VPC network: global, so regions sit inside it
GCP_REGION = "gcp-region"
GCP_SUBNET = "gcp-subnet"
GCP_ROUTER = "cloud-router"
GCP_VPN_GATEWAY = "cloud-vpn-gateway"   # HA VPN or classic (target) VPN gateway
GCP_PEER_GATEWAY = "external-vpn-gateway"  # the on-prem / other-cloud end of a VPN, as GCP records it
GCP_INTERCONNECT = "interconnect"      # a Dedicated Interconnect, or the partner side of a Partner one
GCP_NCC_HUB = "ncc-hub"
GCP_INSTANCE = "gce-instance"

NET_SITE = "site"
NET_DEVICE = "device"
NET_SEGMENT = "segment"      # a multi-access L3 network shared by 3+ devices
NET_EXTERNAL = "external"    # something outside the collected estate (internet, a peer)

NODE_KINDS = frozenset({
    AWS_ACCOUNT, AWS_REGION, AWS_VPC, AWS_AZ, AWS_SUBNET, AWS_IGW, AWS_EIGW, AWS_NAT,
    AWS_ENDPOINT, AWS_TGW, AWS_VGW, AWS_CGW, AWS_DX_CONNECTION, AWS_DX_GATEWAY, AWS_INSTANCE,
    AWS_ORG, AWS_ORG_ROOT, AWS_OU, AWS_ORG_POLICY,
    AWS_BEDROCK, AWS_BEDROCK_MODEL, AWS_IAM, AWS_IAM_ROLE, AWS_IAM_USER, AWS_EKS,
    GCP_ORG, GCP_FOLDER, GCP_PROJECT, GCP_NETWORK, GCP_REGION, GCP_SUBNET, GCP_ROUTER,
    GCP_VPN_GATEWAY, GCP_PEER_GATEWAY, GCP_INTERCONNECT, GCP_NCC_HUB, GCP_INSTANCE,
    NET_SITE, NET_DEVICE, NET_SEGMENT, NET_EXTERNAL,
})

# -- link kinds --------------------------------------------------------------

LINK_PHYSICAL = "physical"            # CDP/LLDP neighbour
LINK_L3 = "l3"                        # two devices with addresses in the same subnet
LINK_SEGMENT = "segment-member"       # device -> segment
LINK_TGW_ATTACHMENT = "tgw-attachment"
LINK_TGW_PEERING = "tgw-peering"
LINK_VPC_PEERING = "vpc-peering"
LINK_VPN = "vpn"                      # customer gateway <-> TGW / VGW
LINK_VGW_ATTACHMENT = "vgw-attachment"
LINK_DX_VIF = "dx-vif"                # DX connection -> DX gateway / VGW
LINK_DXGW_ASSOCIATION = "dxgw-association"
LINK_STITCH = "stitch"                # on-prem device <-> AWS edge, found by merge
LINK_POLICY = "policy-attachment"     # organization policy -> root / OU / account
LINK_TRUST = "iam-trust"              # account or principal -> the role it may assume
LINK_SHARED_VPC = "shared-vpc"        # GCP service project -> its Shared VPC host project
LINK_INTERCONNECT_ATTACHMENT = "interconnect-attachment"  # Cloud Router -> interconnect (a VLAN)
LINK_NCC_SPOKE = "ncc-spoke"          # NCC hub -> the network / VPN / attachment it connects

LINK_KINDS = frozenset({
    LINK_PHYSICAL, LINK_L3, LINK_SEGMENT, LINK_TGW_ATTACHMENT, LINK_TGW_PEERING,
    LINK_VPC_PEERING, LINK_VPN, LINK_VGW_ATTACHMENT, LINK_DX_VIF, LINK_DXGW_ASSOCIATION,
    LINK_STITCH, LINK_POLICY, LINK_TRUST, LINK_SHARED_VPC, LINK_INTERCONNECT_ATTACHMENT,
    LINK_NCC_SPOKE,
})


def node_id(namespace: str, kind: str, *parts: str) -> str:
    """Deterministic node ID, e.g. node_id("aws", "vpc", "vpc-0abc")."""
    return ":".join([namespace, kind, *[str(p) for p in parts]])


def link_id(kind: str, source: str, target: str, source_port: str = "", target_port: str = "",
            key: str = "", directed: bool = False) -> str:
    """Deterministic link ID. Undirected links get the same ID whichever end
    reported them, so a cable seen from both switches is one link. Ports and
    `key` (a VPN or attachment ID) tell apart parallel links between the same
    two nodes."""
    ends = [(source, source_port), (target, target_port)]
    if not directed:
        ends.sort()
    parts = [kind] + [f"{node}[{port}]" if port else node for node, port in ends]
    return "|".join(parts + ([key] if key else []))


@dataclass
class Node:
    id: str
    kind: str
    label: str
    parent: Optional[str] = None
    attrs: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"id": self.id, "kind": self.kind, "label": self.label}
        if self.parent is not None:
            d["parent"] = self.parent
        if self.attrs:
            d["attrs"] = self.attrs
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Node":
        return cls(d["id"], d["kind"], d.get("label", d["id"]), d.get("parent"), dict(d.get("attrs", {})))


@dataclass
class Link:
    id: str
    kind: str
    source: str
    target: str
    label: str = ""
    directed: bool = False
    attrs: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"id": self.id, "kind": self.kind, "source": self.source,
                             "target": self.target}
        if self.label:
            d["label"] = self.label
        if self.directed:
            d["directed"] = True
        if self.attrs:
            d["attrs"] = self.attrs
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Link":
        return cls(d["id"], d["kind"], d["source"], d["target"], d.get("label", ""),
                   bool(d.get("directed", False)), dict(d.get("attrs", {})))


def _merge_attrs(into: Dict[str, Any], new: Dict[str, Any]) -> None:
    """Fill in attributes. Existing non-empty values win over new ones, except
    that lists are unioned and dicts merged, so two partial views of the same
    thing combine instead of overwriting each other."""
    for key, value in new.items():
        if value is None:
            continue
        old = into.get(key)
        if old in (None, "", [], {}):
            into[key] = value
        elif isinstance(old, list) and isinstance(value, list):
            for item in value:
                if item not in old:
                    old.append(item)
        elif isinstance(old, dict) and isinstance(value, dict):
            _merge_attrs(old, value)


class Topology:
    def __init__(self, meta: Optional[Dict[str, Any]] = None):
        self.nodes: Dict[str, Node] = {}
        self.links: Dict[str, Link] = {}
        self.meta: Dict[str, Any] = dict(meta or {})

    # -- building -----------------------------------------------------------

    def add_node(self, id: str, kind: str, label: Optional[str] = None,
                 parent: Optional[str] = None, **attrs: Any) -> Node:
        """Add a node, or fill in an existing one with the same ID."""
        node = self.nodes.get(id)
        if node is None:
            node = self.nodes[id] = Node(id, kind, label or id, parent, {})
        else:
            if label and node.label in (node.id, node.id.split(":")[-1]):
                # A bare ID is a placeholder; any real name replaces it.
                node.label = label
            if parent and node.parent is None:
                node.parent = parent
        _merge_attrs(node.attrs, attrs)
        return node

    def add_link(self, kind: str, source: str, target: str, *, label: str = "",
                 source_port: str = "", target_port: str = "", key: str = "",
                 directed: bool = False, id: Optional[str] = None, **attrs: Any) -> Link:
        """Add a link, or fill in the existing one with the same ID.

        Undirected links are stored with source < target (ports swapped to
        match), so the same cable reported from either end lands on one link
        with consistent `source_port` / `target_port` attributes.
        """
        if not directed and target < source:
            source, target = target, source
            source_port, target_port = target_port, source_port
        lid = id or link_id(kind, source, target, source_port, target_port, key, directed)
        link = self.links.get(lid)
        if link is None:
            link = self.links[lid] = Link(lid, kind, source, target, label, directed, {})
        elif label and not link.label:
            link.label = label
        if source_port:
            attrs.setdefault("source_port", source_port)
        if target_port:
            attrs.setdefault("target_port", target_port)
        _merge_attrs(link.attrs, attrs)
        return link

    def warn(self, message: str) -> None:
        """Record a non-fatal problem (an unreachable device, a denied API call)."""
        warnings: List[str] = self.meta.setdefault("warnings", [])
        if message not in warnings:
            warnings.append(message)

    # -- reading ------------------------------------------------------------

    def children(self, id: Optional[str]) -> List[Node]:
        """Direct children of `id`, or the top-level nodes for None."""
        return sorted((n for n in self.nodes.values() if n.parent == id), key=lambda n: n.id)

    def ancestors(self, id: str) -> List[str]:
        out, seen = [], {id}
        parent = self.nodes[id].parent if id in self.nodes else None
        while parent is not None and parent not in seen:
            out.append(parent)
            seen.add(parent)
            parent = self.nodes[parent].parent if parent in self.nodes else None
        return out

    def links_of(self, id: str) -> List[Link]:
        return sorted((l for l in self.links.values() if id in (l.source, l.target)),
                      key=lambda l: l.id)

    def by_kind(self, *kinds: str) -> List[Node]:
        return sorted((n for n in self.nodes.values() if n.kind in kinds), key=lambda n: n.id)

    def __iter__(self) -> Iterator[Node]:
        return iter(sorted(self.nodes.values(), key=lambda n: n.id))

    def validate(self) -> List[str]:
        """Structural problems: unknown kinds, dangling references, cycles."""
        problems = []
        for n in self.nodes.values():
            if n.kind not in NODE_KINDS:
                problems.append(f"node {n.id}: unknown kind {n.kind!r}")
            if n.parent is not None and n.parent not in self.nodes:
                problems.append(f"node {n.id}: parent {n.parent} does not exist")
            if n.parent is not None and n.id in self.ancestors(n.id):
                problems.append(f"node {n.id}: parent cycle")
        for l in self.links.values():
            if l.kind not in LINK_KINDS:
                problems.append(f"link {l.id}: unknown kind {l.kind!r}")
            for end in (l.source, l.target):
                if end not in self.nodes:
                    problems.append(f"link {l.id}: endpoint {end} does not exist")
        return sorted(problems)

    # -- serialisation ------------------------------------------------------

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema": SCHEMA,
            "meta": self.meta,
            "nodes": [n.to_dict() for n in sorted(self.nodes.values(), key=lambda n: n.id)],
            "links": [l.to_dict() for l in sorted(self.links.values(), key=lambda l: l.id)],
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Topology":
        schema = d.get("schema")
        if schema != SCHEMA:
            raise ValueError(f"unsupported topology schema {schema!r} (expected {SCHEMA})")
        topo = cls(d.get("meta"))
        for nd in d.get("nodes", []):
            node = Node.from_dict(nd)
            topo.nodes[node.id] = node
        for ld in d.get("links", []):
            link = Link.from_dict(ld)
            topo.links[link.id] = link
        return topo

    def dumps(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"

    @classmethod
    def loads(cls, text: str) -> "Topology":
        return cls.from_dict(json.loads(text))

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write(self.dumps())

    @classmethod
    def load(cls, path: str) -> "Topology":
        with open(path, encoding="utf-8") as f:
            return cls.loads(f.read())


def union(topologies: Iterable[Topology]) -> Topology:
    """Plain union by ID (no stitching): the building block for merge."""
    out = Topology()
    sources: List[Any] = []
    for topo in topologies:
        for n in topo.nodes.values():
            out.add_node(n.id, n.kind, n.label, n.parent, **json.loads(json.dumps(n.attrs)))
        for l in topo.links.values():
            out.add_link(l.kind, l.source, l.target, label=l.label, directed=l.directed,
                         id=l.id, **json.loads(json.dumps(l.attrs)))
        sources.append({k: v for k, v in topo.meta.items() if k != "warnings"})
        for w in topo.meta.get("warnings", []):
            out.warn(w)
    out.meta["sources"] = sources
    return out
