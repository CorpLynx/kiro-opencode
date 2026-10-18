"""The words drawn on a diagram: node labels, tooltips and link labels.

Every renderer shows the same text, so it is decided here once. Labels
are plain text; each renderer escapes them for its own format.
"""

from __future__ import annotations

import ipaddress
from typing import Any, List, Tuple

from ..model import (LINK_DX_VIF, LINK_DXGW_ASSOCIATION, LINK_L3, LINK_PHYSICAL, LINK_SEGMENT,
                     LINK_STITCH, LINK_TGW_ATTACHMENT, LINK_TGW_PEERING, LINK_VPC_PEERING,
                     LINK_VPN, Link, Node)

MAX_CHARS = 44
OK_STATES = {"", "available", "up", "active", "attached", "associated", "accepted", "running",
             "pending-acceptance", "connected"}


def clip(text: str, limit: int = MAX_CHARS) -> str:
    """Keep labels a readable width; the full value is in the tooltip."""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _join(values: Any) -> str:
    if isinstance(values, (list, tuple)):
        return ", ".join(str(v) for v in values if v not in (None, ""))
    return "" if values is None else str(values)


def _asn(value: Any) -> str:
    return f"ASN {value}" if value not in (None, "") else ""


def _details(node: Node) -> List[str]:
    a = node.attrs
    k = node.kind
    if k == "account":
        status = str(a.get("status") or "")
        return [_join(a.get("account_id")),
                "management account" if a.get("management") else "",
                status.lower() if status and status.upper() != "ACTIVE" else "",
                _policy_line(a)]
    if k == "organization":
        return [_join(a.get("org_id"))]
    if k in ("org-root", "ou"):
        # Containers show one detail line: the SCPs matter more than the ID.
        return [_policy_line(a), _join(a.get("ou_id") or a.get("root_id"))]
    if k == "org-policy":
        return [str(a.get("type") or "").upper()]
    if k == "region":
        return [_join(a.get("region"))]
    if k == "vpc":
        return [_join(list(a.get("cidrs") or []) + list(a.get("ipv6_cidrs") or []))]
    if k == "subnet":
        return [_join(a.get("cidr")), _join(a.get("ipv6_cidrs"))]
    if k in ("internet-gateway", "egress-only-internet-gateway"):
        return [_join(a.get("gateway_id"))]
    if k == "nat-gateway":
        return [_join(a.get("nat_gateway_id")), _join(a.get("public_ips") or a.get("private_ips"))]
    if k == "vpc-endpoint":
        return [_join(a.get("endpoint_id")), _join(a.get("service")), _join(a.get("endpoint_type"))]
    if k == "transit-gateway":
        return [_join(a.get("tgw_id")), _asn(a.get("asn"))]
    if k == "vpn-gateway":
        return [_join(a.get("vgw_id")), _asn(a.get("asn"))]
    if k == "customer-gateway":
        return [_join(a.get("ip")), _asn(a.get("asn"))]
    if k == "dx-connection":
        return [_join(a.get("connection_id")), _join(a.get("location")), _join(a.get("bandwidth"))]
    if k == "dx-gateway":
        return [_join(a.get("dxgw_id")), _asn(a.get("asn"))]
    if k == "instance":
        ips = a.get("private_ips") or []
        return [_join(ips[:1] if isinstance(ips, list) else ips), _join(a.get("instance_type"))]
    if k == "device":
        return [_join(a.get("mgmt_ip")), _join(a.get("model") or a.get("platform"))]
    if k == "segment":
        return [_join(a.get("subnet"))]
    return []


def _policy_line(attrs: Any) -> str:
    """The SCPs on an organization node, the policies people ask about first."""
    scps = (attrs.get("policies") or {}).get("scp") if isinstance(attrs.get("policies"), dict) else None
    return f"SCP: {_join(scps)}" if scps else ""


def node_title(node: Node) -> str:
    return clip(node.label or node.id)


def node_lines(node: Node) -> List[str]:
    """Title first, then the attributes a person looks for (CIDR, ID,
    management address, model), skipping anything the title already says."""
    title = node_title(node)
    lines = [title]
    for line in _details(node):
        line = clip(line)
        if line and line not in title and line not in lines:
            lines.append(line)
    return lines


def tooltip(node: Node) -> str:
    """Everything simple about a node, one `key: value` per line. Bulky
    structures (interfaces, routes) are summarised by count."""
    out = [node.id, f"kind: {node.kind}"]
    for key in sorted(node.attrs):
        value = node.attrs[key]
        if value in (None, "", [], {}):
            continue
        if isinstance(value, dict):
            if all(not isinstance(v, (dict, list)) for v in value.values()):
                value = ", ".join(f"{k}={v}" for k, v in sorted(value.items()))
            elif all(_scalar_or_list(v) for v in value.values()):
                value = "; ".join(f"{k}={_join(v)}" for k, v in sorted(value.items()))
            else:
                value = f"{len(value)} entries"
        elif isinstance(value, list):
            if all(not isinstance(v, (dict, list)) for v in value):
                value = ", ".join(str(v) for v in value)
            else:
                value = f"{len(value)} entries"
        out.append(f"{key}: {value}")
    return "\n".join(out)


def _scalar_or_list(value: Any) -> bool:
    """A dict value a tooltip can show inline: a scalar, or a list of them
    (organization policies are {"scp": ["DenyRegions", ...]})."""
    if isinstance(value, list):
        return all(not isinstance(v, (dict, list)) for v in value)
    return not isinstance(value, dict)


def link_tooltip(link: Link) -> str:
    out = [f"{link.kind}: {link.source} – {link.target}"]
    for key in sorted(link.attrs):
        value = link.attrs[key]
        if value in (None, "", [], {}):
            continue
        if isinstance(value, dict):
            value = ", ".join(f"{k}={v}" for k, v in sorted(value.items())
                              if not isinstance(v, (dict, list)))
        elif isinstance(value, list):
            value = (", ".join(str(v) for v in value)
                     if all(not isinstance(v, (dict, list)) for v in value) else f"{len(value)} entries")
        out.append(f"{key}: {value}")
    return "\n".join(out)


def _tunnels(tunnels: Any) -> str:
    if not isinstance(tunnels, list) or not tunnels:
        return ""
    up = sum(1 for t in tunnels if isinstance(t, dict) and str(t.get("status", "")).upper() == "UP")
    return f"{up}/{len(tunnels)} tunnels up"


def _middle(link: Link) -> List[str]:
    a = link.attrs
    k = link.kind
    if k == LINK_PHYSICAL:
        return [_join(a.get("subnets"))]
    if k == LINK_L3:
        vrf = a.get("vrf")
        return [_join(a.get("subnet")), f"vrf {vrf}" if vrf else ""]
    if k == LINK_SEGMENT:
        return [_join(a.get("address"))]
    if k in (LINK_TGW_ATTACHMENT, LINK_TGW_PEERING):
        return [_join(a.get("attachment_id"))]
    if k == LINK_VPC_PEERING:
        return [_join(a.get("peering_id"))]
    if k == LINK_VPN:
        routing = str(a.get("routing") or "").upper()
        detail = " · ".join(x for x in (routing, _tunnels(a.get("tunnels"))) if x)
        return [_join(a.get("vpn_id")), detail]
    if k == LINK_DX_VIF:
        vlan = a.get("vlan")
        detail = " · ".join(x for x in (f"VLAN {vlan}" if vlan not in (None, "") else "",
                                             _join(a.get("vif_type"))) if x)
        return [_join(a.get("vif_id")), detail]
    if k == LINK_DXGW_ASSOCIATION:
        return [_join(a.get("association_id"))]
    if k == LINK_STITCH:
        return [" ".join(x for x in (_join(a.get("rule")), _join(a.get("address"))) if x)]
    return []


_IF_SHORT = (("HundredGigE", "Hu"), ("FortyGigabitEthernet", "Fo"), ("TwentyFiveGigE", "Twe"),
             ("TenGigabitEthernet", "Te"), ("GigabitEthernet", "Gi"), ("FastEthernet", "Fa"),
             ("Port-Channel", "Po"), ("Ethernet", "Eth"), ("Loopback", "Lo"), ("Tunnel", "Tu"),
             ("Management", "Mgmt"))


def short_port(name: str) -> str:
    """Cisco-style short interface names, so port labels fit on a link;
    the full name stays in the tooltip."""
    for full, short in _IF_SHORT:
        if name.startswith(full) and name[len(full):len(full) + 1].isdigit():
            return short + name[len(full):]
    return name


def _end(link: Link, node_id: str, port_key: str) -> str:
    port = short_port(str(link.attrs.get(port_key) or ""))
    if link.kind == LINK_L3:
        addr = (link.attrs.get("addresses") or {}).get(node_id) if isinstance(
            link.attrs.get("addresses"), dict) else None
        if addr:
            return clip(f"{port} .{_host_part(str(addr))}" if port else str(addr), 28)
    return clip(port, 28)


def _host_part(addr: str) -> str:
    """Last octet / hextet: the subnet is already in the middle label."""
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return addr
    return str(ip).split(".")[-1] if ip.version == 4 else str(ip).split(":")[-1]


def link_labels(link: Link) -> Tuple[List[str], str, str]:
    """(middle lines, label at the source end, label at the target end)."""
    middle: List[str] = []
    if link.label:
        middle.append(clip(link.label))
    for line in _middle(link):
        line = clip(line)
        if line and all(line not in m for m in middle):
            middle.append(line)
    state = str(link.attrs.get("state") or "")
    if state.lower() not in OK_STATES:
        middle.append(f"[{clip(state, 20)}]")
    return middle, _end(link, link.source, "source_port"), _end(link, link.target, "target_port")
