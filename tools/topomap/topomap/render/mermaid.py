"""Mermaid flowchart output, for Markdown wikis and pull requests.

Containers become nested `subgraph`s; Mermaid lays the picture out
itself. IDs are reduced to `[A-Za-z0-9_]` (with a prefix so words like
`end` can't break the syntax) and all text uses Mermaid's `#...;` entity
codes, so no label can close a bracket or a quote early.
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Optional

from ..model import Topology
from . import labels, styles

SHAPES = {
    "router": '(("{}"))',
    "firewall": '{{{{"{}"}}}}',
    "external": '(["{}"])',
    "segment": '[/"{}"/]',
    "instance": '("{}")',
    "server": '("{}")',
    "phone": '("{}")',
    "wireless-ap": '("{}")',
    "vpc-endpoint": '>"{}"]',
}
GATEWAYS = {"internet-gateway", "egress-only-internet-gateway", "nat-gateway", "vpn-gateway",
            "customer-gateway", "transit-gateway", "dx-connection", "dx-gateway"}
ARROWS = {  # (undirected, directed)
    "l3": ("-.-", "-.->"), "vpn": ("-.-", "-.->"), "stitch": ("-.-", "-.->"),
    "vpc-peering": ("-.-", "-.->"), "tgw-peering": ("-.-", "-.->"), "iam-trust": ("-.-", "-.->"),
    "dx-vif": ("===", "==>"), "dxgw-association": ("===", "==>"),
}
CLASSES = {
    "aws": "fill:#F4EFFF,stroke:#8C4FFF,color:#232F3E",
    "org": "fill:#FDF0F6,stroke:#E7157B,color:#232F3E",
    "gcp": "fill:#E8F0FE,stroke:#4285F4,color:#202124",
    "ec2": "fill:#FFF1E5,stroke:#ED7100,color:#232F3E",
    "net": "fill:#EAF2F7,stroke:#005073,color:#232F3E",
    "fw": "fill:#F6DEDA,stroke:#B03A2E,color:#232F3E",
    "ext": "fill:#EEEEEE,stroke:#8A929C,color:#232F3E,stroke-dasharray:4 3",
    "ai": "fill:#E6F6F3,stroke:#01A88D,color:#232F3E",
    "iam": "fill:#FCE9EC,stroke:#DD344C,color:#232F3E",
}


def text(value: str) -> str:
    """Mermaid entity-code escaping; '#' first so later codes survive."""
    out = str(value).replace("#", "#35;")
    for ch, code in (("&", "#amp;"), ("<", "#lt;"), (">", "#gt;"), ('"', "#quot;"),
                     ("|", "#124;"), ("[", "#91;"), ("]", "#93;"), ("{", "#123;"),
                     ("}", "#125;"), ("`", "#96;")):
        out = out.replace(ch, code)
    return out.replace("\n", " ")


def _ids(topo: Topology) -> Dict[str, str]:
    out: Dict[str, str] = {}
    used = set()
    for nid in sorted(topo.nodes):
        base = "n_" + re.sub(r"[^A-Za-z0-9_]", "_", nid)
        candidate, i = base, 2
        while candidate in used:
            candidate, i = f"{base}_{i}", i + 1
        used.add(candidate)
        out[nid] = candidate
    return out


def _class_of(vclass: str) -> Optional[str]:
    if vclass in ("instance", "eks-cluster"):
        return "ec2"
    if vclass == "bedrock-model":
        return "ai"
    if vclass in ("iam-role", "iam-user"):
        return "iam"
    if vclass in ("account-leaf", "account-management", "org-policy"):
        return "org"
    if vclass in ("cloud-router", "cloud-vpn-gateway", "external-vpn-gateway", "interconnect",
                  "partner-interconnect", "ncc-hub", "gce-instance", "gcp-project-leaf",
                  "gcp-project-keys"):
        return "gcp"
    if vclass in GATEWAYS or vclass == "vpc-endpoint":
        return "aws"
    if vclass == "firewall":
        return "fw"
    if vclass == "external":
        return "ext"
    if vclass in styles.SHAPES:
        return "net"
    return None


def render(topo: Topology, direction: str = "TB", title: str = "", link_labels: bool = True) -> str:
    parents = styles.effective_parents(topo)
    vclass = styles.classify(topo, parents)
    ids = _ids(topo)
    kids: Dict[Optional[str], List[str]] = {}
    for nid in sorted(topo.nodes):
        kids.setdefault(parents[nid], []).append(nid)

    lines: List[str] = []
    if title:
        lines += ["---", f"title: {json.dumps(title)}", "---"]
    lines.append(f"flowchart {direction}")
    members: Dict[str, List[str]] = {}

    def emit(nid: str, depth: int) -> None:
        pad = "  " * depth
        label = "<br>".join(text(l) for l in labels.node_lines(topo.nodes[nid]))
        if styles.is_container_class(vclass[nid]):
            lines.append(f'{pad}subgraph {ids[nid]}["{label}"]')
            for k in kids.get(nid, []):
                emit(k, depth + 1)
            lines.append(f"{pad}end")
            return
        shape = SHAPES.get(vclass[nid])
        if shape is None:
            shape = '[["{}"]]' if vclass[nid] in GATEWAYS else '["{}"]'
        lines.append(f"{pad}{ids[nid]}{shape.format(label)}")
        cls = _class_of(vclass[nid])
        if cls:
            members.setdefault(cls, []).append(ids[nid])

    for nid in kids.get(None, []):
        emit(nid, 1)

    def chain(nid: str) -> List[str]:
        out = [nid]
        while parents[out[-1]] is not None:
            out.append(parents[out[-1]])  # type: ignore[arg-type]
        return out

    for link in sorted(topo.links.values(), key=lambda l: l.id):
        if link.source not in ids or link.target not in ids:
            continue
        if link.source in chain(link.target) or link.target in chain(link.source):
            continue  # a link to its own container is shown by nesting
        plain, directed = ARROWS.get(link.kind, ("---", "-->"))
        arrow = directed if link.directed else plain
        middle = labels.link_labels(link)[0] if link_labels else []
        label = f"|{'<br>'.join(text(l) for l in middle)}|" if middle else ""
        lines.append(f"  {ids[link.source]} {arrow}{label} {ids[link.target]}")

    for cls in sorted(members):
        lines.append(f"  classDef {cls} {CLASSES[cls]}")
        lines.append(f"  class {','.join(members[cls])} {cls}")
    return "\n".join(lines) + "\n"
