"""draw.io (diagrams.net) output.

Written as uncompressed XML so it diffs in git and can be redacted as
text. Containers are group cells and children use coordinates relative
to their parent cell, as draw.io expects; edges live in the default layer
with `source`/`target` set, so draw.io keeps them attached when shapes
are moved. Cell IDs are the topology IDs, so a diff between two renders
of the same estate shows what changed rather than renumbered cells.

The shape styles are copied from draw.io's own sidebars
(`Sidebar-AWS4.js` for `mxgraph.aws4.*`, `Sidebar-Cisco19.js` for
`mxgraph.cisco19.*`), so cells look and behave exactly like ones dragged
from the AWS 2021 and Cisco 19 palettes.
"""

from __future__ import annotations

from typing import Dict, List

from ..aws_services import service_info
from ..model import Topology
from . import labels
from .layout import Box, Layout, Route

# -- styles copied from draw.io's sidebars --------------------------------------

_AWS_NET = ("sketch=0;outlineConnect=0;fontColor=#232F3E;gradientColor=none;fillColor=#8C4FFF;"
            "strokeColor=none;dashed=0;verticalLabelPosition=bottom;verticalAlign=top;align=center;"
            "html=1;fontSize=12;fontStyle=0;aspect=fixed;pointerEvents=1;shape=mxgraph.aws4.")
_AWS_NET_RESOURCE = (
    "sketch=0;points=[[0,0,0],[0.25,0,0],[0.5,0,0],[0.75,0,0],[1,0,0],[0,1,0],[0.25,1,0],"
    "[0.5,1,0],[0.75,1,0],[1,1,0],[0,0.25,0],[0,0.5,0],[0,0.75,0],[1,0.25,0],[1,0.5,0],"
    "[1,0.75,0]];outlineConnect=0;fontColor=#232F3E;fillColor=#8C4FFF;strokeColor=#ffffff;"
    "dashed=0;verticalLabelPosition=bottom;verticalAlign=top;align=center;html=1;fontSize=12;"
    "fontStyle=0;aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.")
# The service icons draw.io's own AWS 2021 sidebar uses for IAM and EKS.
IAM_LOGO = (_AWS_NET_RESOURCE.replace("fillColor=#8C4FFF", "fillColor=#DD344C")
            + "identity_and_access_management;")
BEDROCK_LOGO = _AWS_NET_RESOURCE.replace("fillColor=#8C4FFF", "fillColor=#01A88D") + "bedrock;"
CORNER_LOGOS = {"iam": IAM_LOGO, "bedrock": BEDROCK_LOGO}   # container class -> its icon
LOGO_BOX = (8, 6, 30, 30)          # x, y, width, height inside the container's title bar
_AWS_MGMT = ("sketch=0;outlineConnect=0;fontColor=#232F3E;gradientColor=none;fillColor=#E7157B;"
             "strokeColor=none;dashed=0;verticalLabelPosition=bottom;verticalAlign=top;align=center;"
             "html=1;fontSize=12;fontStyle=0;aspect=fixed;pointerEvents=1;shape=mxgraph.aws4.")
_AWS_COMPUTE = ("sketch=0;outlineConnect=0;fontColor=#232F3E;gradientColor=none;fillColor=#ED7100;"
                "strokeColor=none;dashed=0;verticalLabelPosition=bottom;verticalAlign=top;"
                "align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;pointerEvents=1;"
                "shape=mxgraph.aws4.")
_AWS_GROUP = (
    "points=[[0,0],[0.25,0],[0.5,0],[0.75,0],[1,0],[1,0.25],[1,0.5],[1,0.75],[1,1],[0.75,1],"
    "[0.5,1],[0.25,1],[0,1],[0,0.75],[0,0.5],[0,0.25]];outlineConnect=0;gradientColor=none;"
    "html=1;whiteSpace=wrap;fontSize=12;fontStyle=0;container=1;pointerEvents=0;collapsible=0;"
    "recursiveResize=0;shape=mxgraph.aws4.group;grIcon=mxgraph.aws4.")
_PLAIN_GROUP_TAIL = "container=1;pointerEvents=0;collapsible=0;recursiveResize=0;"

_CISCO_SQUARE = (
    "sketch=0;points=[[0.015,0.015,0],[0.985,0.015,0],[0.985,0.985,0],[0.015,0.985,0],[0.25,0,0],"
    "[0.5,0,0],[0.75,0,0],[1,0.25,0],[1,0.5,0],[1,0.75,0],[0.75,1,0],[0.5,1,0],[0.25,1,0],"
    "[0,0.75,0],[0,0.5,0],[0,0.25,0]];verticalLabelPosition=bottom;html=1;verticalAlign=top;"
    "aspect=fixed;align=center;pointerEvents=1;shape=mxgraph.cisco19.")
_CISCO_ROUND = (
    "sketch=0;points=[[0.5,0,0],[1,0.5,0],[0.5,1,0],[0,0.5,0],[0.145,0.145,0],[0.8555,0.145,0],"
    "[0.855,0.8555,0],[0.145,0.855,0]];verticalLabelPosition=bottom;html=1;verticalAlign=top;"
    "aspect=fixed;align=center;pointerEvents=1;shape=mxgraph.cisco19.")
_CISCO_FREE = ("verticalLabelPosition=bottom;sketch=0;html=1;verticalAlign=top;aspect=fixed;"
               "align=center;pointerEvents=1;shape=mxgraph.cisco19.")
_CISCO_COLORS = "fillColor=#FAFAFA;strokeColor=#005073;"

VERTEX_STYLES: Dict[str, str] = {
    # AWS / Network & Content Delivery, AWS / Compute
    "internet-gateway": _AWS_NET + "internet_gateway;",
    "egress-only-internet-gateway": _AWS_NET + "internet_gateway;",
    "nat-gateway": _AWS_NET + "nat_gateway;",
    "vpc-endpoint": _AWS_NET + "endpoints;",
    "vpn-gateway": _AWS_NET + "vpn_gateway;",
    "customer-gateway": _AWS_NET + "customer_gateway;",
    "dx-gateway": _AWS_NET + "gateway;",
    "transit-gateway": _AWS_NET_RESOURCE + "transit_gateway;",
    "dx-connection": _AWS_NET_RESOURCE + "direct_connect;",
    "instance": _AWS_COMPUTE + "instance2;",
    "eks-cluster": _AWS_NET_RESOURCE.replace("fillColor=#8C4FFF", "fillColor=#ED7100") + "eks;",
    # AWS / Machine Learning, Security
    "bedrock-model": "rounded=1;whiteSpace=wrap;html=1;fillColor=#E6F6F3;strokeColor=#01A88D;fontColor=#232F3E;",
    "iam-role": IAM_LOGO,   # the same IAM icon as the corner of the IAM box
    "iam-user": _AWS_NET.replace("fillColor=#8C4FFF", "fillColor=#232F3D") + "user;",
    # AWS / Management & Governance
    "account-leaf": _AWS_MGMT + "organizations_account;",
    "account-management": _AWS_MGMT + "organizations_management_account;",
    "org-policy": "rounded=1;whiteSpace=wrap;html=1;fillColor=#FDF0F6;strokeColor=#E7157B;fontColor=#232F3E;",
    # Cisco 19
    "router": _CISCO_ROUND + "rect;prIcon=router;" + _CISCO_COLORS,
    "switch-core": _CISCO_SQUARE + "rect;prIcon=l3_switch;" + _CISCO_COLORS,
    "switch-access": _CISCO_SQUARE + "rect;prIcon=l2_switch;" + _CISCO_COLORS,
    "firewall": _CISCO_SQUARE + "rect;prIcon=firewall;" + _CISCO_COLORS,
    "load-balancer": _CISCO_SQUARE + "rect;prIcon=load_balancer;" + _CISCO_COLORS,
    "wireless-controller": _CISCO_SQUARE + "rect;prIcon=wireless_lan_controller;" + _CISCO_COLORS,
    "wireless-ap": ("points=[[0.03,0.36,0],[0.18,0,0],[0.5,0.34,0],[0.82,0,0],[0.97,0.36,0],"
                    "[1,0.67,0],[0.975,0.975,0],[0.5,1,0],[0.025,0.975,0],[0,0.67,0]];"
                    + _CISCO_FREE + "wireless_access_point2;fillColor=#fafafa;strokeColor=#005073;"),
    "server": _CISCO_SQUARE + "server2;fillColor=#fafafa;strokeColor=#005073;",
    "phone": ("points=[[0.59,0,0],[0.87,0.015,0],[0.88,0.3,0],[0.99,0.99,0],[0.5,1,0],[0.01,0.99,0],"
              "[0.075,0.5,0],[0.17,0.115,0]];" + _CISCO_FREE
              + "ip_phone2;fillColor=#fafafa;strokeColor=#005073;"),
    "external": ("points=[[0,0.64,0],[0.2,0.15,0],[0.4,0.01,0],[0.79,0.25,0],[1,0.65,0],"
                 "[0.8,0.86,0],[0.41,1,0],[0.16,0.86,0]];" + _CISCO_FREE
                 + "cloud;fillColor=#A6A6A6;strokeColor=none;"),
    # neutral shapes, label inside
    "segment": "rounded=1;whiteSpace=wrap;html=1;fillColor=#dae8fc;strokeColor=#6c8ebf;arcSize=40;",
    "device": "rounded=1;whiteSpace=wrap;html=1;fillColor=#f5f5f5;strokeColor=#666666;fontColor=#333333;",
    "generic": "rounded=1;whiteSpace=wrap;html=1;fillColor=#f5f5f5;strokeColor=#666666;fontColor=#333333;",
}

CONTAINER_STYLES: Dict[str, str] = {
    # AWS / Groups
    "organization": _AWS_GROUP + "group_aws_cloud_alt;strokeColor=#232F3E;fillColor=none;"
                                 "verticalAlign=top;align=left;spacingLeft=30;fontColor=#232F3E;"
                                 "dashed=0;",
    "org-root": ("fillColor=none;strokeColor=#E7157B;dashed=1;verticalAlign=top;fontStyle=1;"
                 "fontColor=#E7157B;whiteSpace=wrap;html=1;align=left;spacingLeft=10;"
                 + _PLAIN_GROUP_TAIL),
    "ou": ("fillColor=none;strokeColor=#E7157B;dashed=0;verticalAlign=top;fontStyle=1;"
           "fontColor=#B0084D;whiteSpace=wrap;html=1;align=left;spacingLeft=10;"
           + _PLAIN_GROUP_TAIL),
    "account": _AWS_GROUP + "group_account;strokeColor=#CD2264;fillColor=none;verticalAlign=top;"
                            "align=left;spacingLeft=30;fontColor=#CD2264;dashed=0;",
    "region": _AWS_GROUP + "group_region;strokeColor=#00A4A6;fillColor=none;verticalAlign=top;"
                           "align=left;spacingLeft=30;fontColor=#147EBA;dashed=1;",
    "vpc": _AWS_GROUP + "group_vpc2;strokeColor=#8C4FFF;fillColor=none;verticalAlign=top;"
                        "align=left;spacingLeft=30;fontColor=#AAB7B8;dashed=0;",
    "iam": ("fillColor=#FEF5F6;strokeColor=#DD344C;dashed=0;verticalAlign=top;fontStyle=1;"
            "fontColor=#B02A3D;whiteSpace=wrap;html=1;align=left;spacingLeft=46;"
            + _PLAIN_GROUP_TAIL),
    "bedrock": ("fillColor=#F0FAF8;strokeColor=#01A88D;dashed=0;verticalAlign=top;fontStyle=1;"
                "fontColor=#017A66;whiteSpace=wrap;html=1;align=left;spacingLeft=46;"
                + _PLAIN_GROUP_TAIL),
    "az": ("fillColor=none;strokeColor=#147EBA;dashed=1;verticalAlign=top;fontStyle=0;"
           "fontColor=#147EBA;whiteSpace=wrap;html=1;" + _PLAIN_GROUP_TAIL),
    "subnet-public": _AWS_GROUP + "group_security_group;grStroke=0;strokeColor=#7AA116;"
                                  "fillColor=#F2F6E8;verticalAlign=top;align=left;spacingLeft=30;"
                                  "fontColor=#248814;dashed=0;",
    "subnet-private": _AWS_GROUP + "group_security_group;grStroke=0;strokeColor=#00A4A6;"
                                   "fillColor=#E6F6F7;verticalAlign=top;align=left;spacingLeft=30;"
                                   "fontColor=#147EBA;dashed=0;",
    "group": ("fillColor=none;strokeColor=#5A6C86;dashed=1;verticalAlign=top;fontStyle=0;"
              "fontColor=#5A6C86;whiteSpace=wrap;html=1;" + _PLAIN_GROUP_TAIL),
    "aws-service": ("fillColor=none;strokeColor=#7D8998;dashed=1;verticalAlign=top;fontStyle=1;"
                    "fontColor=#232F3E;whiteSpace=wrap;html=1;align=left;spacingLeft=46;"
                    + _PLAIN_GROUP_TAIL),
}

_EDGE_BASE = "html=1;rounded=0;labelBackgroundColor=#FFFFFF;fontSize=10;"
EDGE_STYLES: Dict[str, str] = {
    "segment-member": "strokeColor=#6C8EBF;strokeWidth=1.5;",
    "tgw-attachment": "strokeColor=#8C4FFF;strokeWidth=2;",
    "tgw-peering": "strokeColor=#8C4FFF;strokeWidth=2;dashed=1;dashPattern=8 4;",
    "vpc-peering": "strokeColor=#8C4FFF;strokeWidth=1.5;dashed=1;dashPattern=8 4;",
    "vgw-attachment": "strokeColor=#8C4FFF;strokeWidth=1.5;",
    "vpn": "strokeColor=#C98B00;strokeWidth=2;dashed=1;",
    "dx-vif": "strokeColor=#1A73E8;strokeWidth=4;",
    "dxgw-association": "strokeColor=#1A73E8;strokeWidth=2;",
    "policy-attachment": "strokeColor=#E7157B;strokeWidth=1;dashed=1;dashPattern=4 3;",
    "iam-trust": "strokeColor=#DD344C;strokeWidth=1.5;dashed=1;dashPattern=6 3;",
    "related": "strokeColor=#879196;strokeWidth=1;dashed=1;dashPattern=3 3;",
}
_END_LABEL = "edgeLabel;html=1;align=center;verticalAlign=middle;resizable=0;points=[];fontSize=9;"
_TITLE = "text;html=1;align=left;verticalAlign=middle;fontSize=18;fontStyle=1;"


def xml_attr(text: str) -> str:
    """Escape for a double-quoted XML attribute, keeping newlines."""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;").replace("'", "&#39;").replace("\r", "")
            .replace("\n", "&#10;").replace("\t", "&#9;"))


def html_text(text: str) -> str:
    """Escape label text for draw.io's html=1 labels."""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def service_icon(service: str) -> str:
    """draw.io's AWS 2021 icon for a service, in the service's category colour."""
    _, icon, color, _ = service_info(service)
    return _AWS_NET_RESOURCE.replace("fillColor=#8C4FFF", f"fillColor={color}") + f"{icon};"


def _logo_style(box: Box, topo: Topology) -> str:
    if box.vclass == "aws-service":
        return service_icon(str(topo.nodes[box.id].attrs.get("service") or ""))
    return CORNER_LOGOS.get(box.vclass, "")


def vertex_style(box: Box, topo: Topology, parent_class: str = "") -> str:
    if box.vclass == "aws-resource":
        style = service_icon(str(topo.nodes[box.id].attrs.get("service") or ""))
        if not parent_class.startswith("subnet"):
            style += "labelBackgroundColor=#FFFFFF;"
        return style
    if box.container:
        style = CONTAINER_STYLES.get(box.vclass, CONTAINER_STYLES["group"])
    else:
        style = VERTEX_STYLES.get(box.vclass, VERTEX_STYLES["generic"])
        if "verticalLabelPosition=bottom" in style and not parent_class.startswith("subnet"):
            style += "labelBackgroundColor=#FFFFFF;"   # keeps the label readable over links
    if topo.nodes[box.id].attrs.get("external"):
        style += "dashed=1;opacity=70;"
    return style


def _label(box: Box) -> str:
    lines = [html_text(l) for l in box.lines]
    if box.container:
        return "<br>".join(lines[:2])
    return "<br>".join([f"<b>{lines[0]}</b>"] + lines[1:])


def _vertex(box: Box, parent_box, topo: Topology) -> str:
    if box.container:
        x, y, w, h = box.x, box.y, box.w, box.h
    else:
        x, y, w, h = box.icon
    if parent_box is not None:
        x, y = x - parent_box.x, y - parent_box.y
    parent = parent_box.id if parent_box is not None else "1"
    parent_class = parent_box.vclass if parent_box is not None else ""
    node = topo.nodes[box.id]
    return (f'        <UserObject label="{xml_attr(_label(box))}" '
            f'tooltip="{xml_attr(labels.tooltip(node))}" topomapper_kind="{xml_attr(node.kind)}" '
            f'id="{xml_attr(box.id)}">\n'
            f'          <mxCell style="{xml_attr(vertex_style(box, topo, parent_class))}" vertex="1" '
            f'parent="{xml_attr(parent)}">\n'
            f'            <mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry" />\n'
            f'          </mxCell>\n'
            f'        </UserObject>')


def _logo(box: Box, style: str) -> str:
    """The service icon in the corner of an IAM, Bedrock or service box.
    draw.io's group shapes only take a fixed set of corner icons, so the
    logo is its own cell."""
    x, y, w, h = LOGO_BOX
    return (f'        <mxCell id="{xml_attr(box.id)}#logo" value="" '
            f'style="{xml_attr(style)}" '
            f'vertex="1" connectable="0" parent="{xml_attr(box.id)}">\n'
            f'          <mxGeometry x="{x}" y="{y}" width="{w}" height="{h}" as="geometry" />\n'
            f'        </mxCell>')


def _edge(route: Route, link_labels: bool) -> List[str]:
    link = route.link
    style = _EDGE_BASE + EDGE_STYLES.get(link.kind, "strokeColor=#666666;")
    style += "endArrow=classic;" if link.directed else "endArrow=none;"
    middle, at_source, at_target = labels.link_labels(link) if link_labels else ([], "", "")
    value = "<br>".join(html_text(l) for l in middle)
    eid = xml_attr(link.id)
    geometry = '            <mxGeometry relative="1" as="geometry" />'
    if route.waypoint is not None:
        wx, wy = route.waypoint
        geometry = ('            <mxGeometry relative="1" as="geometry">\n'
                    f'              <Array as="points"><mxPoint x="{wx}" y="{wy}" /></Array>\n'
                    '            </mxGeometry>')
    out = [f'        <UserObject label="{xml_attr(value)}" tooltip="{xml_attr(labels.link_tooltip(link))}" '
           f'topomapper_kind="{xml_attr(link.kind)}" id="{eid}">\n'
           f'          <mxCell style="{xml_attr(style)}" edge="1" parent="1" '
           f'source="{xml_attr(link.source)}" target="{xml_attr(link.target)}">\n'
           f'{geometry}\n'
           f'          </mxCell>\n'
           f'        </UserObject>']
    ends = (("src", at_source, round(-1 + 2 * route.source_t, 3)),
            ("dst", at_target, round(1 - 2 * route.target_t, 3)))
    for suffix, text, pos in ends:
        if text:
            out.append(f'        <mxCell id="{eid}#{suffix}" value="{xml_attr(html_text(text))}" '
                       f'style="{xml_attr(_END_LABEL)}" vertex="1" connectable="0" parent="{eid}">\n'
                       f'          <mxGeometry x="{pos}" relative="1" as="geometry">'
                       f'<mxPoint as="offset" /></mxGeometry>\n'
                       f'        </mxCell>')
    return out


def render(topo: Topology, lay: Layout, link_labels: bool = True) -> str:
    name = lay.title or "topology"
    cells = ['        <mxCell id="0" />', '        <mxCell id="1" parent="0" />']
    if lay.title:
        cells.append(f'        <mxCell id="topomapper-title" value="{xml_attr(html_text(lay.title))}" '
                     f'style="{xml_attr(_TITLE)}" vertex="1" parent="1">\n'
                     f'          <mxGeometry x="20" y="16" width="{max(lay.width - 40, 100)}" '
                     f'height="30" as="geometry" />\n'
                     f'        </mxCell>')
    # Links first, so node labels (with their white background) sit on top.
    for route in lay.routes:
        cells.extend(_edge(route, link_labels))
    for nid in lay.order:
        box = lay.boxes[nid]
        parent_box = lay.boxes.get(box.parent) if box.parent else None
        cells.append(_vertex(box, parent_box, topo))
        logo = _logo_style(box, topo) if box.container else ""
        if logo:
            cells.append(_logo(box, logo))
    return ('<mxfile host="topomapper" agent="topomapper" compressed="false">\n'
            f'  <diagram id="topomapper" name="{xml_attr(name)}">\n'
            f'    <mxGraphModel dx="{lay.width}" dy="{lay.height}" grid="1" gridSize="10" '
            f'guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" '
            f'pageWidth="{lay.width}" pageHeight="{lay.height}" math="0" shadow="0">\n'
            '      <root>\n'
            + "\n".join(cells) + "\n"
            '      </root>\n'
            '    </mxGraphModel>\n'
            '  </diagram>\n'
            '</mxfile>\n')
