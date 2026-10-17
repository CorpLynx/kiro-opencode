"""What we learn about one network device, and how a set of devices becomes
a topology.

Parsers (saved command output, config backups) and the live SSH crawler
both produce `DeviceFacts`; `facts_to_topology` is the one place that turns
them into nodes and links, so both paths draw the same picture.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence

from .model import (LINK_L3, LINK_PHYSICAL, LINK_SEGMENT, NET_DEVICE, NET_SEGMENT, NET_SITE,
                    Topology, node_id)

ROLES = ("router", "switch", "firewall", "load-balancer", "wireless", "server", "phone", "unknown")


@dataclass
class Interface:
    name: str                                            # full name, see normalize_ifname
    addresses: List[str] = field(default_factory=list)   # CIDR strings: "10.0.0.1/30", "2001:db8::1/64"
    description: str = ""
    status: str = ""                                     # "up", "down", "admin-down"
    vrf: str = ""


@dataclass
class Neighbor:
    local_port: str
    remote_name: str
    remote_port: str = ""
    remote_mgmt_ip: Optional[str] = None
    remote_platform: str = ""
    remote_capabilities: List[str] = field(default_factory=list)  # e.g. ["router", "switch"]
    protocol: str = ""                                            # "cdp" or "lldp"


@dataclass
class DeviceFacts:
    hostname: str
    vendor: str = ""          # "cisco", "arista", "juniper", ...
    platform: str = ""        # netmiko-style device type: "cisco_ios", "cisco_nxos", ...
    model: str = ""
    os_version: str = ""
    serial: str = ""
    mgmt_ip: Optional[str] = None
    role: str = ""            # one of ROLES, or "" to let guess_role decide
    site: str = ""
    interfaces: List[Interface] = field(default_factory=list)
    neighbors: List[Neighbor] = field(default_factory=list)
    source: str = ""          # where the facts came from: a file path, "ssh://host"


# -- normalisation -------------------------------------------------------------

# Abbreviation -> full name, longest first so "Te" doesn't shadow "TwentyFiveGigE".
_IF_PREFIXES = [
    ("hundredgige", "HundredGigE"), ("hu", "HundredGigE"),
    ("fortygigabitethernet", "FortyGigabitEthernet"), ("fo", "FortyGigabitEthernet"),
    ("twentyfivegige", "TwentyFiveGigE"), ("twe", "TwentyFiveGigE"),
    ("tengigabitethernet", "TenGigabitEthernet"), ("te", "TenGigabitEthernet"),
    ("gigabitethernet", "GigabitEthernet"), ("gige", "GigabitEthernet"), ("gi", "GigabitEthernet"),
    ("fastethernet", "FastEthernet"), ("fa", "FastEthernet"),
    ("ethernet", "Ethernet"), ("eth", "Ethernet"), ("et", "Ethernet"),
    ("port-channel", "Port-Channel"), ("portchannel", "Port-Channel"), ("po", "Port-Channel"),
    ("management", "Management"), ("mgmt", "mgmt"), ("ma", "Management"),
    ("loopback", "Loopback"), ("lo", "Loopback"),
    ("vlan", "Vlan"), ("vl", "Vlan"),
    ("tunnel", "Tunnel"), ("tu", "Tunnel"),
]
_IF_RE = re.compile(r"^([A-Za-z][A-Za-z-]*?)\s*(\d.*)$")


def normalize_ifname(name: str) -> str:
    """Expand Cisco/Arista-style abbreviations so both ends of a CDP/LLDP
    report agree: "Gi1/0/1" -> "GigabitEthernet1/0/1", "Eth1/1" ->
    "Ethernet1/1". Junos-style names ("ge-0/0/0", "xe-0/0/1.0", "ae0")
    are returned unchanged."""
    name = name.strip()
    m = _IF_RE.match(name)
    if not m:
        return name
    prefix, rest = m.group(1).lower(), m.group(2)
    for abbrev, full in _IF_PREFIXES:
        if prefix == abbrev:
            return full + rest
    return name


def normalize_hostname(name: str) -> str:
    """Lower-case and drop the domain: CDP reports "core1.example.net" for a
    device whose own prompt says "core1". Also strips the "(serial)" suffix
    NX-OS appends in CDP."""
    name = re.sub(r"\(.*\)$", "", name.strip()).strip()
    try:
        ipaddress.ip_address(name)
        return name  # an address, not a name: keep the dots
    except ValueError:
        pass
    return name.split(".")[0].lower()


def device_id(hostname: str) -> str:
    return node_id("net", NET_DEVICE, normalize_hostname(hostname))


def guess_role(platform: str = "", capabilities: Sequence[str] = (), hostname: str = "") -> str:
    """Best-effort role from a platform string, CDP/LLDP capabilities and
    naming conventions. Returns "unknown" rather than guessing wildly."""
    text = f"{platform} {hostname}".lower()
    caps = {c.lower() for c in capabilities}
    if re.search(r"\b(asa\d|asa\b|firepower|ftd|fortigate|palo ?alto|pa-\d|srx|checkpoint)|fw\d|-fw", text):
        return "firewall"
    if re.search(r"\b(f5|big-?ip|netscaler|citrix adc)\b|lb\d", text):
        return "load-balancer"
    if re.search(r"\b(air-|aironet|ap\d|wlc|catalyst 9800)", text) or "wlan" in caps:
        return "wireless"
    if re.search(r"\bphone\b|\bcp-\d", text) or "phone" in caps:
        return "phone"
    if "router" in caps or re.search(r"\b(isr|asr|csr|c8\d{3}|mx\d|ptx|router)|rtr|-r\d", text):
        return "router"
    if caps & {"switch", "bridge"} or re.search(
            r"\b(nexus|n\dk|catalyst|ws-c|c9\d{3}|dcs-|qfx|ex\d)|sw\d", text):
        return "switch"
    if "station" in caps or "host" in caps:
        return "server"
    return "unknown"


# -- facts -> topology ----------------------------------------------------------

def facts_to_topology(facts: Iterable[DeviceFacts], infer_l3: bool = True,
                      l3_exclude: Sequence[str] = ()) -> Topology:
    """Build a topology from device facts.

    * Every device becomes a node (inside a site container if it has one).
    * Every CDP/LLDP neighbour becomes a physical link. Neighbours that
      weren't collected themselves become device nodes marked
      `discovered: "neighbor"`.
    * With `infer_l3`, interface addresses are grouped by subnet: a subnet
      shared by exactly two devices becomes an `l3` link (unless the two are
      already cabled together, in which case the cable gets the subnet), and
      one shared by three or more becomes a segment node. Loopbacks, /32 and
      /128 host routes and any subnet inside `l3_exclude` are skipped.
    """
    facts = list(facts)
    topo = Topology({"collector": "facts", "devices": len(facts)})
    excluded = [ipaddress.ip_network(n, strict=False) for n in l3_exclude]
    collected = set()

    for f in facts:
        did = device_id(f.hostname)
        collected.add(did)
        parent = None
        if f.site:
            parent = node_id("net", NET_SITE, f.site.lower())
            topo.add_node(parent, NET_SITE, f.site)
        topo.add_node(
            did, NET_DEVICE, normalize_hostname(f.hostname), parent,
            vendor=f.vendor, platform=f.platform, model=f.model, os_version=f.os_version,
            serial=f.serial, mgmt_ip=f.mgmt_ip,
            role=f.role or guess_role(f"{f.model} {f.platform}", (), f.hostname),
            interfaces=[{"name": normalize_ifname(i.name), "addresses": list(i.addresses),
                         "description": i.description, "status": i.status, "vrf": i.vrf}
                        for i in f.interfaces],
            sources=[f.source] if f.source else None,
        )

    for f in facts:
        did = device_id(f.hostname)
        for n in f.neighbors:
            rid = device_id(n.remote_name)
            if rid == did:
                continue
            if rid not in collected:
                topo.add_node(rid, NET_DEVICE, normalize_hostname(n.remote_name),
                              discovered="neighbor", platform=n.remote_platform,
                              mgmt_ip=n.remote_mgmt_ip,
                              role=guess_role(n.remote_platform, n.remote_capabilities, n.remote_name))
            topo.add_link(LINK_PHYSICAL, did, rid,
                          source_port=normalize_ifname(n.local_port),
                          target_port=normalize_ifname(n.remote_port) if n.remote_port else "",
                          protocols=[n.protocol] if n.protocol else None)

    if infer_l3:
        _infer_l3(topo, facts, excluded)
    return topo


def _infer_l3(topo: Topology, facts: List[DeviceFacts], excluded) -> None:
    members: Dict[ipaddress._BaseNetwork, List[tuple]] = {}
    for f in facts:
        did = device_id(f.hostname)
        for iface in f.interfaces:
            name = normalize_ifname(iface.name)
            if name.lower().startswith("loopback") or name.lower().startswith("lo"):
                continue
            for addr in iface.addresses:
                try:
                    ip = ipaddress.ip_interface(addr)
                except ValueError:
                    continue
                net = ip.network
                if net.prefixlen >= net.max_prefixlen or ip.ip.is_link_local:
                    continue
                if any(net.version == ex.version and net.subnet_of(ex) for ex in excluded):
                    continue
                members.setdefault(net, []).append((did, name, str(ip.ip), iface.vrf))

    for net, devs in sorted(members.items(), key=lambda kv: (kv[0].version, kv[0])):
        devices = sorted({d for d, _, _, _ in devs})
        if len(devices) < 2:
            continue
        if len(devices) == 2:
            first: Dict[str, tuple] = {}
            for entry in sorted(devs):
                first.setdefault(entry[0], entry)
            (a, a_port, a_ip, vrf), (b, b_port, b_ip, _) = first[devices[0]], first[devices[1]]
            cabled = [l for l in topo.links.values() if l.kind == LINK_PHYSICAL
                      and {l.source, l.target} == {a, b}
                      and _port_of(l, a) == a_port and _port_of(l, b) == b_port]
            if cabled:
                for l in cabled:
                    l.attrs.setdefault("subnets", [])
                    if str(net) not in l.attrs["subnets"]:
                        l.attrs["subnets"].append(str(net))
                continue
            topo.add_link(LINK_L3, a, b, source_port=a_port, target_port=b_port,
                          label=str(net), subnet=str(net), vrf=vrf or None,
                          addresses={a: a_ip, b: b_ip})
        else:
            sid = node_id("net", NET_SEGMENT, str(net))
            topo.add_node(sid, NET_SEGMENT, str(net), subnet=str(net))
            for did, port, ip, vrf in devs:
                topo.add_link(LINK_SEGMENT, did, sid, source_port=port, address=ip,
                              vrf=vrf or None)


def _port_of(link, node: str) -> str:
    return link.attrs.get("source_port", "") if link.source == node else link.attrs.get("target_port", "")
