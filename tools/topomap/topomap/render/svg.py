"""Standalone SVG output, from the same layout as draw.io.

No external images or fonts: each node class has a small glyph drawn in
its own coordinate system and scaled into the icon box, so the file opens
anywhere (browser, wiki, ticket) without Graphviz or draw.io. Every node
and link carries a `<title>` (the hover tooltip) and a `data-id` with its
topology ID.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Tuple

from ..model import Topology
from . import labels, styles
from .layout import EDGE_LINE_H, LINE_H, Box, Layout, Route, edge_text_width

FONT = "Helvetica, Arial, sans-serif"
INK = "#232F3E"
CISCO = "#005073"
CISCO_FILL = "#EAF2F7"


def esc(text: str) -> str:
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;"))


# -- glyphs: (view width, view height, body) ------------------------------------

def _aws_tile(text: str, color: str) -> Tuple[int, int, str]:
    size = 15 if len(text) <= 3 else 12
    return (48, 48, f'<rect x="1" y="1" width="46" height="46" rx="6" fill="{color}"/>'
                    f'<text x="24" y="{24 + size * 0.36:.1f}" text-anchor="middle" font-size="{size}" '
                    f'font-weight="bold" fill="#FFFFFF">{esc(text)}</text>')


_ARROW = 'fill="{c}" stroke="none"'

GLYPHS: Dict[str, Callable[[], Tuple[int, int, str]]] = {
    "internet-gateway": lambda: _aws_tile("IGW", "#8C4FFF"),
    "egress-only-internet-gateway": lambda: _aws_tile("EIGW", "#8C4FFF"),
    "nat-gateway": lambda: _aws_tile("NAT", "#8C4FFF"),
    "vpc-endpoint": lambda: _aws_tile("VPCE", "#8C4FFF"),
    "vpn-gateway": lambda: _aws_tile("VGW", "#8C4FFF"),
    "customer-gateway": lambda: _aws_tile("CGW", "#8C4FFF"),
    "transit-gateway": lambda: _aws_tile("TGW", "#8C4FFF"),
    "dx-connection": lambda: _aws_tile("DX", "#8C4FFF"),
    "dx-gateway": lambda: _aws_tile("DXGW", "#8C4FFF"),
    "instance": lambda: _aws_tile("EC2", "#ED7100"),
    "cloud-router": lambda: _aws_tile("RTR", "#4285F4"),
    "cloud-vpn-gateway": lambda: _aws_tile("VPN", "#4285F4"),
    "external-vpn-gateway": lambda: _aws_tile("PEER", "#5F6368"),
    "interconnect": lambda: _aws_tile("IC", "#4285F4"),
    "partner-interconnect": lambda: _aws_tile("PIC", "#4285F4"),
    "ncc-hub": lambda: _aws_tile("NCC", "#34A853"),
    "gce-instance": lambda: _aws_tile("VM", "#4285F4"),
    "gcp-project-leaf": lambda: _aws_tile("PRJ", "#1967D2"),
    "gcp-project-keys": lambda: _aws_tile("KMS", "#D93025"),
    "eks-cluster": lambda: _aws_tile("EKS", "#ED7100"),
    "iam-role": lambda: _aws_tile("IAM", "#DD344C"),   # matches the IAM box's corner tile
    "iam-user": lambda: _aws_tile("USER", "#232F3D"),
    "account-leaf": lambda: _aws_tile("ACCT", "#E7157B"),
    "account-management": lambda: _aws_tile("MGMT", "#E7157B"),
    "router": lambda: (50, 50,
        f'<circle cx="25" cy="25" r="23" fill="{CISCO_FILL}" stroke="{CISCO}" stroke-width="2"/>'
        f'<path d="M25 6 l-6 7 h12z M25 44 l-6 -7 h12z M21 25 l-8 -6 v12z M29 25 l8 -6 v12z" '
        f'{_ARROW.format(c=CISCO)}/>'
        f'<path d="M25 13 v8 M25 29 v8 M6 25 h7 M37 25 h7" stroke="{CISCO}" stroke-width="3"/>'),
    "switch-core": lambda: (50, 50,
        f'<rect x="2" y="2" width="46" height="46" rx="5" fill="{CISCO_FILL}" stroke="{CISCO}" '
        f'stroke-width="3"/>'
        f'<path d="M9 17 h24 M41 33 h-24" stroke="{CISCO}" stroke-width="3"/>'
        f'<path d="M41 17 l-8 -6 v12z M9 33 l8 -6 v12z" {_ARROW.format(c=CISCO)}/>'
        f'<text x="25" y="47" text-anchor="middle" font-size="8" fill="{CISCO}">L3</text>'),
    "switch-access": lambda: (50, 50,
        f'<rect x="2" y="2" width="46" height="46" rx="5" fill="{CISCO_FILL}" stroke="{CISCO}" '
        f'stroke-width="2"/>'
        f'<path d="M9 17 h24 M41 33 h-24" stroke="{CISCO}" stroke-width="3"/>'
        f'<path d="M41 17 l-8 -6 v12z M9 33 l8 -6 v12z" {_ARROW.format(c=CISCO)}/>'),
    "firewall": lambda: (64, 50,
        '<rect x="2" y="2" width="60" height="46" rx="3" fill="#F6DEDA" stroke="#B03A2E" '
        'stroke-width="2"/>'
        '<path d="M2 17 h60 M2 33 h60 M22 2 v15 M42 2 v15 M12 17 v16 M32 17 v16 M52 17 v16 '
        'M22 33 v15 M42 33 v15" stroke="#B03A2E" stroke-width="1.5"/>'),
    "load-balancer": lambda: (64, 50,
        f'<rect x="2" y="2" width="60" height="46" rx="5" fill="{CISCO_FILL}" stroke="{CISCO}" '
        f'stroke-width="2"/>'
        f'<path d="M10 25 h14 M24 25 L46 12 M24 25 H46 M24 25 L46 38" stroke="{CISCO}" '
        f'stroke-width="2.5" fill="none"/>'
        f'<circle cx="24" cy="25" r="4" fill="{CISCO}"/>'),
    "wireless-controller": lambda: (64, 50,
        f'<rect x="2" y="14" width="60" height="34" rx="4" fill="{CISCO_FILL}" stroke="{CISCO}" '
        f'stroke-width="2"/>'
        f'<path d="M20 10 a16 16 0 0 1 24 0 M25 13 a8 8 0 0 1 14 0" stroke="{CISCO}" '
        f'stroke-width="2" fill="none"/>'
        f'<text x="32" y="36" text-anchor="middle" font-size="11" font-weight="bold" '
        f'fill="{CISCO}">WLC</text>'),
    "wireless-ap": lambda: (50, 50,
        f'<rect x="8" y="30" width="34" height="16" rx="8" fill="{CISCO_FILL}" stroke="{CISCO}" '
        f'stroke-width="2"/>'
        f'<path d="M9 18 a20 20 0 0 1 32 0 M15 23 a12 12 0 0 1 20 0" stroke="{CISCO}" '
        f'stroke-width="2.5" fill="none"/><circle cx="25" cy="27" r="2.5" fill="{CISCO}"/>'),
    "server": lambda: (28, 50,
        f'<rect x="2" y="2" width="24" height="46" rx="2" fill="{CISCO_FILL}" stroke="{CISCO}" '
        f'stroke-width="2"/>'
        f'<path d="M6 12 h16 M6 19 h16 M6 26 h16" stroke="{CISCO}" stroke-width="2"/>'
        f'<circle cx="14" cy="40" r="2.5" fill="{CISCO}"/>'),
    "phone": lambda: (58, 50,
        f'<rect x="10" y="20" width="38" height="28" rx="4" fill="{CISCO_FILL}" stroke="{CISCO}" '
        f'stroke-width="2"/>'
        f'<path d="M8 14 q21 -16 42 0 l-6 6 q-15 -9 -30 0z" fill="{CISCO_FILL}" stroke="{CISCO}" '
        f'stroke-width="2"/>'
        + "".join(f'<circle cx="{20 + 9 * c}" cy="{28 + 7 * r}" r="2" fill="{CISCO}"/>'
                  for r in range(3) for c in range(3))),
    "external": lambda: (60, 36,
        '<path d="M14 34 a10 10 0 0 1 -1 -20 a13 13 0 0 1 24 -6 a10 10 0 0 1 15 6 '
        'a10 10 0 0 1 -2 20z" fill="#C9CED6" stroke="#8A929C" stroke-width="1.5"/>'),
}
CORNER_TILES = {"iam": ("IAM", "#DD344C"), "bedrock": ("AI", "#01A88D")}
BOXED = {"segment": ("#DAE8FC", "#6C8EBF"), "device": ("#F5F5F5", "#666666"),
         "org-policy": ("#FDF0F6", "#E7157B"),
         "bedrock-model": ("#E6F6F3", "#01A88D"),
         "generic": ("#F5F5F5", "#666666")}


def _glyph(box: Box) -> str:
    x, y, w, h = box.icon
    if box.vclass in BOXED or box.vclass not in GLYPHS:
        fill, stroke = BOXED.get(box.vclass, BOXED["generic"])
        rx = 16 if box.vclass == "segment" else 6
        return (f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" fill="{fill}" '
                f'stroke="{stroke}" stroke-width="1.5"/>')
    vw, vh, body = GLYPHS[box.vclass]()
    return (f'<g transform="translate({x} {y}) scale({w / vw:.4g} {h / vh:.4g})">{body}</g>')


HALO = ' stroke="#FFFFFF" stroke-width="3" stroke-linejoin="round" paint-order="stroke"'


def _text_lines(lines: List[str], cx: float, y: float, first_bold: bool, size: int = 12,
                color: str = INK, anchor: str = "middle", halo: bool = False) -> str:
    out = []
    for i, line in enumerate(lines):
        weight = ' font-weight="bold"' if first_bold and i == 0 else ""
        out.append(f'<text x="{cx:g}" y="{y + i * LINE_H:g}" text-anchor="{anchor}" '
                   f'font-size="{size}"{weight} fill="{color}"{HALO if halo else ""}>'
                   f'{esc(line)}</text>')
    return "".join(out)


def _container(box: Box, topo: Topology) -> str:
    stroke, fill, dashed, title = styles.CONTAINER_COLORS.get(box.vclass,
                                                             styles.CONTAINER_COLORS["group"])
    if topo.nodes[box.id].attrs.get("external"):
        dashed = True
    dash = ' stroke-dasharray="6 4"' if dashed else ""
    tip = esc(labels.tooltip(topo.nodes[box.id]))
    tab = f'<rect x="{box.x}" y="{box.y}" width="8" height="8" fill="{stroke}"/>'
    logo, text_x = "", box.x + 14
    if box.vclass in CORNER_TILES:
        vw, vh, body = _aws_tile(*CORNER_TILES[box.vclass])
        logo = (f'<g transform="translate({box.x + 8} {box.y + 6}) scale({30 / vw:.4g} {30 / vh:.4g})">'
                f'{body}</g>')
        text_x = box.x + 46
    return (f'<g class="container {esc(box.vclass)}" data-id="{esc(box.id)}"><title>{tip}</title>'
            f'<rect x="{box.x}" y="{box.y}" width="{box.w}" height="{box.h}" fill="{fill}" '
            f'stroke="{stroke}" stroke-width="1.5"{dash}/>{tab}{logo}'
            + _text_lines(box.lines[:2], text_x, box.y + 19, True, 12, title, "start")
            + '</g>')


def _leaf(box: Box, topo: Topology) -> str:
    tip = esc(labels.tooltip(topo.nodes[box.id]))
    x, y, w, h = box.icon
    opacity = ' opacity="0.7"' if topo.nodes[box.id].attrs.get("external") else ""
    if box.vclass in BOXED or box.vclass not in GLYPHS:
        n = len(box.lines)
        text = _text_lines(box.lines, x + w / 2, y + h / 2 - (n - 1) * LINE_H / 2 + 4, True)
    else:
        text = _text_lines(box.lines, x + w / 2, y + h + 15, True, halo=True)
    return (f'<g class="node {esc(box.vclass)}" data-id="{esc(box.id)}"{opacity}>'
            f'<title>{tip}</title>{_glyph(box)}{text}</g>')


def _edge(route: Route) -> str:
    link = route.link
    look = styles.link_look(link)
    dash = f' stroke-dasharray="{look.dash}"' if look.dash else ""
    marker = ' marker-end="url(#arrow)"' if link.directed else ""
    d = "M" + " L".join(f"{x} {y}" for x, y in route.points)
    return (f'<g class="link {esc(link.kind)}" data-id="{esc(link.id)}">'
            f'<title>{esc(labels.link_tooltip(link))}</title>'
            f'<path d="{d}" fill="none" stroke="{look.color}" stroke-width="{look.width:g}"'
            f'{dash}{marker}/></g>')


def _label_box(lines: List[str], cx: float, cy: float, size: int) -> str:
    w = max(edge_text_width(l) for l in lines)
    h = len(lines) * EDGE_LINE_H + 2
    top = cy - h / 2
    out = [f'<rect x="{cx - w / 2:.1f}" y="{top:.1f}" width="{w}" height="{h}" rx="2" '
           f'fill="#FFFFFF" fill-opacity="0.9"/>']
    for i, line in enumerate(lines):
        out.append(f'<text x="{cx:.1f}" y="{top + 11 + i * EDGE_LINE_H:.1f}" text-anchor="middle" '
                   f'font-size="{size}" fill="#333333">{esc(line)}</text>')
    return "".join(out)


def _edge_labels(route: Route) -> str:
    middle, at_source, at_target = labels.link_labels(route.link)
    pts = route.points
    out = []
    if middle:
        if route.waypoint is not None:
            mx, my = route.waypoint
        else:
            mx, my = (pts[0][0] + pts[-1][0]) / 2, (pts[0][1] + pts[-1][1]) / 2
        out.append(_label_box(middle, mx, my, 10))
    if at_source:
        out.append(_label_box([at_source], *route.source_label, 9))
    if at_target:
        out.append(_label_box([at_target], *route.target_label, 9))
    if not out:
        return ""
    return f'<g class="link-label" data-id="{esc(route.link.id)}">{"".join(out)}</g>'


def render(topo: Topology, lay: Layout, link_labels: bool = True) -> str:
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{lay.width}" height="{lay.height}" '
        f'viewBox="0 0 {lay.width} {lay.height}" font-family="{FONT}">',
        '<defs><marker id="arrow" viewBox="0 0 10 10" refX="10" refY="5" markerUnits="userSpaceOnUse" '
        'markerWidth="12" markerHeight="12" orient="auto-start-reverse"><path d="M0 0 L10 5 L0 10z" fill="#333333"/>'
        '</marker></defs>',
        f'<rect x="0" y="0" width="{lay.width}" height="{lay.height}" fill="#FFFFFF"/>',
    ]
    if lay.title:
        parts.append(f'<text x="20" y="38" font-size="18" font-weight="bold" fill="{INK}">'
                     f'{esc(lay.title)}</text>')
    boxes = [lay.boxes[n] for n in lay.order]
    parts.extend(_container(b, topo) for b in boxes if b.container)
    parts.extend(_edge(r) for r in lay.routes)
    parts.extend(_leaf(b, topo) for b in boxes if not b.container)
    if link_labels:
        parts.extend(filter(None, (_edge_labels(r) for r in lay.routes)))
    parts.append("</svg>")
    return "\n".join(parts) + "\n"
