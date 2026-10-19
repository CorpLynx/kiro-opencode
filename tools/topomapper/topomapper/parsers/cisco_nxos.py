"""Cisco NX-OS output: `show version`, `show lldp neighbors detail` and the
per-VRF `show ip interface vrf all` / `show ipv6 interface vrf all`.
CDP detail is read by the IOS parser, which handles both spellings."""

from __future__ import annotations

import re
from typing import Dict, List

from ..facts import Interface, Neighbor
from .cisco_ios import lldp_neighbor
from .common import to_cidr


def parse_version(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    patterns = {
        "hostname": r"^\s*Device name:\s*(\S+)",
        "os_version": r"^\s*(?:NXOS|NX-OSv?|system|kickstart):\s+version\s+(\S+)",
        "model": r"^\s*cisco (Nexus\s?\S+(?: \S+)*?)\s+(?:[Cc]hassis|\(|$)",
        "serial": r"Processor [Bb]oard ID (\S+)",
    }
    for key, pattern in patterns.items():
        m = re.search(pattern, text, re.M)
        if m:
            out[key] = m.group(1).strip()
    return out


def parse_lldp_detail(text: str) -> List[Neighbor]:
    """Blocks start at "Chassis id:" and name the local port in "Local Port
    id:"; the other fields are spelled as on IOS."""
    out = []
    for block in re.split(r"^(?=Chassis id:)", text, flags=re.M):
        m = re.search(r"^Local Port id:\s*(\S+)", block, re.M)
        if m:
            out.append(lldp_neighbor(block, m.group(1)))
    return out


_VRF_RE = re.compile(r'^IP(?:v6)? Interface Status for VRF "([^"]+)"', re.M)
_HEAD_RE = re.compile(r"^(\S+), Interface status: (\S+?),", re.M)


def parse_ip_interface(text: str) -> List[Interface]:
    """IPv4 and IPv6 forms share the layout: a VRF header, then blocks
    headed "<name>, Interface status: protocol-up/link-up/admin-up". IPv4
    lines give the address and the subnet's length; IPv6 address lines are
    tagged "[VALID]"."""
    out = []
    marks = sorted([(m.start(), "vrf", m) for m in _VRF_RE.finditer(text)]
                   + [(m.start(), "head", m) for m in _HEAD_RE.finditer(text)], key=lambda t: t[0])
    vrf = ""
    for i, (pos, kind, m) in enumerate(marks):
        if kind == "vrf":
            vrf = "" if m.group(1) == "default" else m.group(1)
            continue
        end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
        block = text[m.end():end]
        iface = Interface(m.group(1), status=_status(m.group(2)), vrf=vrf)
        for addr, plen in re.findall(r"IP address:\s*(\d+\.\d+\.\d+\.\d+),\s*IP subnet:\s*\S+/(\d+)",
                                     block):
            _append(iface, to_cidr(addr, plen))
        for addr in re.findall(r"^\s+(?:IPv6 address:\s+)?([0-9A-Fa-f:]+/\d{1,3})\s+\[[A-Z]+\]",
                               block, re.M):
            _append(iface, to_cidr(addr))
        out.append(iface)
    return out


def _append(iface: Interface, cidr) -> None:
    if cidr and cidr not in iface.addresses:
        iface.addresses.append(cidr)


def _status(text: str) -> str:
    parts = text.lower().split("/")
    if "admin-down" in parts:
        return "admin-down"
    return "up" if parts and parts[0] == "protocol-up" else "down"
