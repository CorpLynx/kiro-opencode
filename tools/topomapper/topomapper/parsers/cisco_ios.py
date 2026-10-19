"""Cisco IOS / IOS-XE output: `show version`, `show cdp neighbors detail`
(also NX-OS's variant), `show lldp neighbors detail` and the IOS-style
`show ip interface` / `show ipv6 interface` blocks that Arista copies."""

from __future__ import annotations

import re
from typing import Dict, List

from ..facts import Interface, Neighbor
from .common import capabilities, is_ip, pick_port, to_cidr


def parse_version(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    m = re.search(r"^(?!Kernel )(\S+) uptime is ", text, re.M)
    if m:
        out["hostname"] = m.group(1)
    m = (re.search(r"^Cisco IOS XE Software, Version (\S+)", text, re.M)
         or re.search(r"Cisco IOS Software.*?, Version ([^,\s]+)", text))
    if m:
        out["os_version"] = m.group(1)
    m = (re.search(r"^Model [Nn]umber\s*:\s*(\S+)", text, re.M)
         or re.search(r"^[Cc]isco (\S+) \(.*?\) (?:processor )?with ", text, re.M))
    if m:
        out["model"] = m.group(1)
    m = (re.search(r"^System [Ss]erial [Nn]umber\s*:\s*(\S+)", text, re.M)
         or re.search(r"^Processor board ID (\S+)", text, re.M))
    if m:
        out["serial"] = m.group(1)
    return out


# -- CDP ----------------------------------------------------------------------

def parse_cdp_detail(text: str) -> List[Neighbor]:
    """IOS ("Device ID: x", "Entry address(es)") and NX-OS ("Device ID:x",
    "Interface address(es)", "Mgmt address(es)") differ only in spelling,
    so one tolerant parser reads both. The advertised management address
    is preferred over the interface address, since that is what a crawler
    should connect to."""
    out = []
    for block in re.split(r"^(?=Device ID:)", text, flags=re.M):
        m = re.match(r"Device ID:\s*(.+?)\s*$", block, re.M)
        ports = re.search(r"^Interface:\s*([^,]+?)\s*,\s*Port ID \(outgoing port\):\s*(.+?)\s*$",
                          block, re.M)
        if not m or not ports:
            continue
        platform, caps = "", []
        pm = re.search(r"^Platform:\s*(.*?)\s*,\s*Capabilities:\s*(.*?)\s*$", block, re.M)
        if pm:
            platform = re.sub(r"^cisco\s+(?=\S*\d)", "", pm.group(1))
            caps = capabilities(pm.group(2))
        out.append(Neighbor(ports.group(1), m.group(1), ports.group(2), _cdp_address(block),
                            platform, caps, "cdp"))
    return out


def _cdp_address(block: str):
    for heading in (r"(?:Management|Mgmt) address\(es\):", r"(?:Entry|Interface) address\(es\):"):
        m = re.search(heading + r"[ \t]*\n((?:[ \t]+\S.*\n?)+)", block)
        if m:
            for addr in re.findall(r"IP(?:v4|v6)? [Aa]ddress:\s*(\S+)", m.group(1)):
                if is_ip(addr):
                    return addr
    return None


# -- LLDP ---------------------------------------------------------------------

def parse_lldp_detail(text: str) -> List[Neighbor]:
    """IOS blocks are separated by dashed lines; field order varies between
    releases, so each block is read as a whole."""
    out = []
    for block in re.split(r"^-{20,}\s*$", text, flags=re.M):
        local = _field(block, "Local Intf")
        if not local:
            continue
        out.append(lldp_neighbor(block, local))
    return out


def lldp_neighbor(block: str, local: str) -> Neighbor:
    """One IOS or NX-OS LLDP block (they share field names) as a Neighbor."""
    name = _field(block, "System Name")
    if not name or "not advertised" in name:
        name = _field(block, "Chassis id")
    port = pick_port(_field(block, "Port id"), _field(block, "Port Description"))
    caps = _field(block, "Enabled Capabilities")
    if not caps or "not advertised" in caps:
        caps = _field(block, "System Capabilities")
    return Neighbor(local, name, port, _lldp_address(block), _system_description(block),
                    capabilities(caps, codes=True), "lldp")


def _field(block: str, key: str) -> str:
    m = re.search(rf"^\s*{re.escape(key)}\s*:[ \t]*(.*?)\s*$", block, re.M | re.I)
    return m.group(1).strip() if m else ""


def _system_description(block: str) -> str:
    """The first line of the description, which may start on the next line."""
    m = re.search(r"^System Description:[ \t]*(.*)\n(.*)", block, re.M)
    if not m:
        return ""
    text = (m.group(1).strip() or m.group(2).strip()).strip('"')
    return "" if "not advertised" in text else text


def _lldp_address(block: str):
    ips = re.findall(r"^\s+IP(?:V6)?:\s*(\S+)", block, re.M | re.I)
    ips += re.findall(r"^Management Address(?: IPV6)?:\s*(\S+)", block, re.M)
    v4 = [ip for ip in ips if is_ip(ip) and ":" not in ip]
    v6 = [ip for ip in ips if is_ip(ip) and ":" in ip]
    return (v4 or v6 or [None])[0]


# -- show ip interface / show ipv6 interface ---------------------------------------

_HEAD_RE = re.compile(r"^(\S+) is (administratively down|up|down|deleted)"
                      r"(?:, line protocol is (\S+))?", re.M)


def parse_ip_interface(text: str) -> List[Interface]:
    """IOS and EOS `show ip interface` and `show ipv6 interface`: one block
    per interface headed "<name> is up, line protocol is up". Interfaces
    without addresses are kept, with their status, so the device's port
    list is complete."""
    out = []
    heads = list(_HEAD_RE.finditer(text))
    for head, nxt in zip(heads, heads[1:] + [None]):
        block = text[head.end():nxt.start() if nxt else len(text)]
        iface = Interface(head.group(1), status=_status(head.group(2), head.group(3)))
        for addr, plen in re.findall(
                r"^\s+(?:Internet address is|Secondary address(?:es)?(?: is)?:?)\s+"
                r"(\d+\.\d+\.\d+\.\d+)/(\d+)", block, re.M):
            _append(iface, to_cidr(addr, plen))
        for addr, plen in re.findall(r"^\s+([0-9A-Fa-f:]+:[0-9A-Fa-f:]*), subnet is \S+/(\d+)",
                                     block, re.M):
            _append(iface, to_cidr(addr, plen))
        m = re.search(r'VPN Routing/Forwarding "([^"]+)"', block) or \
            re.search(r"^\s+VRF:?\s+(\S+)", block, re.M)
        if m and m.group(1).lower() != "default":
            iface.vrf = m.group(1)
        m = re.search(r"^\s+Description:\s*(.*?)\s*$", block, re.M)
        if m:
            iface.description = m.group(1)
        out.append(iface)
    return out


def _append(iface: Interface, cidr) -> None:
    if cidr and cidr not in iface.addresses:
        iface.addresses.append(cidr)


def _status(admin: str, protocol) -> str:
    if admin.startswith("administratively"):
        return "admin-down"
    if admin == "up" and (protocol is None or protocol.startswith("up")):
        return "up"
    return "down"
