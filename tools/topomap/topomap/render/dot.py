"""Graphviz DOT output, for `dot -Tsvg` / `-Tpng` or other Graphviz tools.

Containers become nested `subgraph cluster_*` blocks. A link that ends on
a container (a VPC peering, a TGW attachment) is drawn to an invisible
anchor inside that cluster and clipped at its border with `lhead`/`ltail`,
which is what `compound=true` is for.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from ..model import Topology
from . import labels, styles

NODE_ATTRS = {
    "router": 'shape=circle, style=filled, fillcolor="#EAF2F7", color="#005073"',
    "switch-core": 'shape=box, style="filled,bold", fillcolor="#EAF2F7", color="#005073"',
    "switch-access": 'shape=box, style=filled, fillcolor="#EAF2F7", color="#005073"',
    "firewall": 'shape=box3d, style=filled, fillcolor="#F6DEDA", color="#B03A2E"',
    "load-balancer": 'shape=trapezium, style=filled, fillcolor="#EAF2F7", color="#005073"',
    "wireless-controller": 'shape=box, style=filled, fillcolor="#EAF2F7", color="#005073"',
    "wireless-ap": 'shape=ellipse, style=filled, fillcolor="#EAF2F7", color="#005073"',
    "server": 'shape=box, style="filled,rounded", fillcolor="#EAF2F7", color="#005073"',
    "phone": 'shape=box, style="filled,rounded", fillcolor="#EAF2F7", color="#005073"',
    "segment": 'shape=parallelogram, style=filled, fillcolor="#DAE8FC", color="#6C8EBF"',
    "external": 'shape=ellipse, style="filled,dashed", fillcolor="#EEEEEE", color="#8A929C"',
    "instance": 'shape=box, style="filled,rounded", fillcolor="#FFF1E5", color="#ED7100"',
    "eks-cluster": 'shape=box, style="filled,rounded", fillcolor="#FFF1E5", color="#ED7100"',
    "bedrock-model": 'shape=box, style="filled,rounded", fillcolor="#E6F6F3", color="#01A88D"',
    "iam-role": 'shape=box, style="filled,rounded", fillcolor="#FCE9EC", color="#DD344C"',
    "iam-user": 'shape=box, style="filled,rounded", fillcolor="#F2F3F3", color="#232F3D"',
}
AWS_GATEWAY_ATTRS = 'shape=component, style=filled, fillcolor="#F4EFFF", color="#8C4FFF"'
DEFAULT_ATTRS = 'shape=box, style="filled,rounded", fillcolor="#F5F5F5", color="#666666"'


def quote(value: str) -> str:
    """A DOT double-quoted string. `&` is written as an entity because
    Graphviz decodes entities in labels; newlines become `\\n`."""
    out = (str(value).replace("\\", "\\\\").replace('"', '\\"').replace("&", "&amp;")
           .replace("\r", "").replace("\n", "\\n"))
    return f'"{out}"'


def _label(lines: List[str]) -> str:
    return quote("\n".join(lines))


def _dash(look: styles.LinkLook) -> str:
    if not look.dash:
        return "solid"
    return "dotted" if look.dash.startswith(("1 ", "2 ")) else "dashed"


def render(topo: Topology, direction: str = "TB", title: str = "", link_labels: bool = True) -> str:
    parents = styles.effective_parents(topo)
    vclass = styles.classify(topo, parents)
    kids: Dict[Optional[str], List[str]] = {}
    for nid in sorted(topo.nodes):
        kids.setdefault(parents[nid], []).append(nid)
    containers = {n for n in topo.nodes if styles.is_container_class(vclass[n])}

    def chain(nid: str) -> List[str]:
        out = [nid]
        while parents[out[-1]] is not None:
            out.append(parents[out[-1]])  # type: ignore[arg-type]
        return out

    drawn = []
    anchored = {n for n in containers if not kids.get(n)}
    for link in sorted(topo.links.values(), key=lambda l: l.id):
        if link.source not in topo.nodes or link.target not in topo.nodes:
            continue
        if link.source in chain(link.target) or link.target in chain(link.source):
            continue
        drawn.append(link)
        anchored.update(e for e in (link.source, link.target) if e in containers)

    lines = [
        "graph topomap {",
        "  compound=true;",
        f"  rankdir={direction};",
        '  fontname="Helvetica"; fontsize=12; newrank=true;',
        '  node [fontname="Helvetica", fontsize=10];',
        '  edge [fontname="Helvetica", fontsize=9];',
    ]
    if title:
        lines.append(f"  label={quote(title)}; labelloc=t; fontsize=16;")

    def emit(nid: str, depth: int) -> None:
        pad = "  " * depth
        node_lines = labels.node_lines(topo.nodes[nid])
        if nid in containers:
            stroke, fill, dashed, font = styles.CONTAINER_COLORS.get(
                vclass[nid], styles.CONTAINER_COLORS["group"])
            style = ["rounded"] + (["dashed"] if dashed else []) + (["filled"] if fill != "none" else [])
            lines.append(f"{pad}subgraph {quote('cluster_' + nid)} {{")
            lines.append(f"{pad}  label={_label(node_lines[:2])}; labeljust=l;")
            lines.append(f'{pad}  style="{",".join(style)}"; color="{stroke}"; fontcolor="{font}";'
                         + (f' fillcolor="{fill}";' if fill != "none" else ""))
            if nid in anchored:
                lines.append(f'{pad}  {quote("anchor:" + nid)} [shape=point, style=invis, '
                             f'width=0.01, label=""];')
            for k in kids.get(nid, []):
                emit(k, depth + 1)
            lines.append(f"{pad}}}")
            return
        attrs = NODE_ATTRS.get(vclass[nid])
        if attrs is None:
            attrs = AWS_GATEWAY_ATTRS if vclass[nid] in styles.AWS_LEAF_KINDS else DEFAULT_ATTRS
        lines.append(f"{pad}{quote(nid)} [label={_label(node_lines)}, {attrs}];")

    for nid in kids.get(None, []):
        emit(nid, 1)

    for link in drawn:
        look = styles.link_look(link)
        attrs = [f'color="{look.color}"', f"penwidth={look.width:g}", f"style={_dash(look)}"]
        if link.directed:
            attrs.append("dir=forward")
        if link_labels:
            middle, at_source, at_target = labels.link_labels(link)
            if middle:
                attrs.append(f"label={_label(middle)}")
            if at_source:
                attrs.append(f"taillabel={quote(at_source)}")
            if at_target:
                attrs.append(f"headlabel={quote(at_target)}")
        src, dst = link.source, link.target
        if src in containers:
            attrs.append(f"ltail={quote('cluster_' + src)}")
            src = "anchor:" + src
        if dst in containers:
            attrs.append(f"lhead={quote('cluster_' + dst)}")
            dst = "anchor:" + dst
        lines.append(f"  {quote(src)} -- {quote(dst)} [{', '.join(attrs)}];")
    lines.append("}")
    return "\n".join(lines) + "\n"
