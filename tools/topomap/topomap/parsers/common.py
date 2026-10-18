"""Helpers shared by the vendor parsers: address and name clean-up, the
interface table every command writes into, and merging facts about the same
device from several sources."""

from __future__ import annotations

import ipaddress
import os
import re
from typing import Dict, Iterable, List, Optional

from ..facts import DeviceFacts, Interface, Neighbor, normalize_hostname, normalize_ifname

VENDORS = {"cisco_ios": "cisco", "cisco_nxos": "cisco", "arista_eos": "arista",
           "juniper_junos": "juniper"}

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_MORE_RE = re.compile(r"-+\(more(?: \d+%)?\)-+|[ \t]*--More--[ \t]*\x08*", re.I)
_JUNOS_RE_STATE = re.compile(r"^\{(?:master|backup|primary|linecard|line card)(?::\d+)?\}\s*$", re.I)


def clean_text(text: str) -> str:
    """Undo terminal artefacts so captures from a real session parse like
    clean output: CRLF, colour codes, pager prompts with their backspaces,
    and the Junos "{master:0}" lines printed before each prompt."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _ANSI_RE.sub("", text)
    text = _MORE_RE.sub("", text)
    text = re.sub(r".\x08", "", text)
    return "\n".join(l.rstrip() for l in text.split("\n") if not _JUNOS_RE_STATE.match(l.strip()))


def is_error_output(text: str) -> bool:
    """True for the reply a device gives to a command it doesn't know or a
    feature that's off ("% Invalid input", "syntax error"), so the section
    is skipped rather than parsed as empty data."""
    head = [l.strip() for l in text.split("\n") if l.strip()][:3]
    return any(re.match(r"(%|\^$|syntax error|error:|unknown command|invalid command|"
                        r"cdp not enabled|lldp is not enabled)", l, re.I) for l in head)


def to_cidr(address: str, mask: Optional[str] = None) -> Optional[str]:
    """"10.0.0.1" + "255.255.255.252" (or "30") -> "10.0.0.1/30", in the
    canonical lower-case form so the same address from two commands
    compares equal. None if it isn't a valid address."""
    text = address.strip().split("%")[0]
    if mask is not None:
        text = f"{text}/{mask.strip().lstrip('/')}"
    try:
        return str(ipaddress.ip_interface(text))
    except ValueError:
        return None


def is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text.strip())
        return True
    except ValueError:
        return False


_IFNAME_RE = re.compile(r"^[A-Za-z][A-Za-z_-]*\s?\d+(?:[/:.]\d+)*$")


def looks_like_ifname(text: str) -> bool:
    """"Gi1/0/1", "Ethernet49/1", "xe-0/1/0", "eth0" yes; an ifIndex ("513"),
    a MAC or a free-text description no. LLDP port IDs are often one of the
    latter, and only a name lines up with what the other end reports."""
    return bool(_IFNAME_RE.match(text.strip()))


def pick_port(port_id: str, description: str = "") -> str:
    """The LLDP port to record: the port ID if it's an interface name,
    otherwise the port description if that is one, otherwise the ID."""
    port_id, description = port_id.strip().strip('"'), description.strip().strip('"')
    if looks_like_ifname(port_id):
        return port_id
    if looks_like_ifname(description):
        return description
    return port_id or description


_CAP_WORDS = {
    "router": "router", "switch": "switch", "bridge": "bridge", "trans-bridge": "bridge",
    "source-route-bridge": "bridge", "host": "station", "station": "station",
    "phone": "phone", "voip-phone": "phone", "telephone": "phone", "wlan": "wlan",
    "repeater": "repeater",
}
_CAP_CODES = {"r": "router", "b": "bridge", "t": "phone", "w": "wlan", "s": "station",
              "p": "repeater"}


def capabilities(text: str, codes: bool = False) -> List[str]:
    """Normalise "Router Switch IGMP", "Bridge, Router" or LLDP codes "B,R"
    to lower-case words, dropping ones that don't say what the box is."""
    out: List[str] = []
    for word in re.split(r"[\s,]+", text.strip().lower()):
        cap = _CAP_CODES.get(word) if codes else _CAP_WORDS.get(word)
        if cap and cap not in out:
            out.append(cap)
    return out


def fold_junos_unit(name: str) -> str:
    """"xe-0/1/0.0" -> "xe-0/1/0". Junos puts addresses on logical unit 0 of
    a port while LLDP names the port itself; folding unit 0 lets an
    interface's subnet attach to the cable LLDP saw. Other units (VLAN
    sub-interfaces, "irb.100") keep their number."""
    name = name.strip()
    return name[:-2] if name.endswith(".0") and not name[:1].isupper() else name


_MGMT_NAME_RE = re.compile(r"^(mgmt\d+|management\d+(/\d+)*|fxp0|em0|me0|vme)$", re.I)
_MGMT_VRF_RE = re.compile(r"^(mgmt|management|mgmt-vrf|mgmt-intf|oob)$", re.I)


def find_mgmt_ip(interfaces: Iterable[Interface]) -> Optional[str]:
    """The IPv4 address of a dedicated management interface, by name first
    (mgmt0, Management1, fxp0, em0, me0) and then by a management VRF
    ("Mgmt-vrf", "Mgmt-intf"). None if there is no clear one: a guess from
    an ordinary port would mislead the crawler and the drawing."""
    interfaces = list(interfaces)
    for matcher in (lambda i: _MGMT_NAME_RE.match(i.name), lambda i: _MGMT_VRF_RE.match(i.vrf)):
        for iface in interfaces:
            if not matcher(iface):
                continue
            for addr in iface.addresses:
                ip = ipaddress.ip_interface(addr).ip
                if ip.version == 4:
                    return str(ip)
    return None


_SOURCE_SUFFIX_RE = re.compile(
    r"[-_.]?(running-config|startup-config|confg|config|conf|cfg|show|capture|output|backup|run)$",
    re.I)


def hostname_from_source(source: str) -> str:
    """"backups/core1-confg.txt" -> "core1": the last resort when neither a
    prompt, `show version` nor a config names the device."""
    base = os.path.basename(source.rstrip("/")) or source
    base = re.sub(r"^[a-z]+://", "", base)
    while True:
        stem, ext = os.path.splitext(base)
        if not ext or not re.match(r"^\.[A-Za-z]+$", ext) or is_ip(base):
            break
        base = stem
    base = _SOURCE_SUFFIX_RE.sub("", base)
    base = re.sub(r"[-_]\d{4}-?\d{2}-?\d{2}.*$", "", base)
    return base or "unknown"


# -- accumulating ---------------------------------------------------------------

class InterfaceTable:
    """Interfaces keyed by normalised name, so the same port reported by
    several commands ("Eth1/1" in one, "Ethernet1/1" in another) is one
    entry. The first non-empty value of each field wins, so callers feed
    runtime state before configuration."""

    def __init__(self) -> None:
        self._by_name: Dict[str, Interface] = {}

    def add(self, iface: Interface) -> Interface:
        name = normalize_ifname(iface.name)
        into = self._by_name.get(name)
        if into is None:
            into = self._by_name[name] = Interface(name)
        merge_interface(into, iface)
        return into

    def extend(self, interfaces: Iterable[Interface]) -> None:
        for iface in interfaces:
            self.add(iface)

    def values(self) -> List[Interface]:
        return list(self._by_name.values())


def merge_interface(into: Interface, new: Interface) -> None:
    for addr in new.addresses:
        if addr not in into.addresses:
            into.addresses.append(addr)
    into.description = into.description or new.description
    into.status = into.status or new.status
    into.vrf = into.vrf or new.vrf


def add_neighbor(neighbors: List[Neighbor], new: Neighbor) -> None:
    """Add a neighbour unless the same one (local port, remote device,
    protocol) is already known, in which case fill in its blanks. CDP and
    LLDP reports of one cable stay separate, so the link records both
    protocols; their remote ports are reconciled by `reconcile_ports`."""
    key = _neighbor_key(new)
    for old in neighbors:
        if _neighbor_key(old) == key:
            old.remote_port = old.remote_port or new.remote_port
            old.remote_mgmt_ip = old.remote_mgmt_ip or new.remote_mgmt_ip
            old.remote_platform = old.remote_platform or new.remote_platform
            for cap in new.remote_capabilities:
                if cap not in old.remote_capabilities:
                    old.remote_capabilities.append(cap)
            return
    neighbors.append(new)


def _neighbor_key(n: Neighbor):
    return (normalize_ifname(n.local_port), normalize_hostname(n.remote_name), n.protocol)


def reconcile_ports(neighbors: List[Neighbor]) -> None:
    """When CDP and LLDP both report the same cable but disagree on the
    remote port (a phone's LLDP port ID "001122334455:P1" vs CDP "Port 1"),
    use the one that is an interface name for both, so the cable is drawn
    once."""
    by_cable: Dict[tuple, List[Neighbor]] = {}
    for n in neighbors:
        by_cable.setdefault((normalize_ifname(n.local_port), normalize_hostname(n.remote_name)),
                            []).append(n)
    for group in by_cable.values():
        named = [n.remote_port for n in group if looks_like_ifname(n.remote_port)]
        if named:
            for n in group:
                if not looks_like_ifname(n.remote_port):
                    n.remote_port = named[0]


def merge_facts(facts: Iterable[DeviceFacts]) -> List[DeviceFacts]:
    """Combine facts about the same device (same normalised hostname) from
    several files, e.g. a capture and a config backup: scalar fields take
    the first non-empty value, interfaces are unioned by normalised name and
    neighbours by (local port, remote device, protocol). Order is kept, so
    the result is deterministic for a given input order."""
    merged: Dict[str, DeviceFacts] = {}
    tables: Dict[str, InterfaceTable] = {}
    sources: Dict[str, List[str]] = {}
    for f in facts:
        key = normalize_hostname(f.hostname)
        into = merged.get(key)
        if into is None:
            into = merged[key] = DeviceFacts(f.hostname)
            tables[key] = InterfaceTable()
            sources[key] = []
        for attr in ("vendor", "platform", "model", "os_version", "serial", "mgmt_ip", "role", "site"):
            if not getattr(into, attr) and getattr(f, attr):
                setattr(into, attr, getattr(f, attr))
        tables[key].extend(f.interfaces)
        for n in f.neighbors:
            add_neighbor(into.neighbors, Neighbor(**{**vars(n), "remote_capabilities": list(n.remote_capabilities)}))
        if f.source and f.source not in sources[key]:
            sources[key].append(f.source)
    for key, f in merged.items():
        f.interfaces = tables[key].values()
        f.mgmt_ip = f.mgmt_ip or find_mgmt_ip(f.interfaces)
        f.source = "; ".join(sources[key])
        reconcile_ports(f.neighbors)
    return list(merged.values())


def fill_from_neighbors(facts: Iterable[DeviceFacts]) -> None:
    """Give a device known only from its config the model its CDP
    neighbours report (CDP "Platform" is the model), so its role can be
    guessed. LLDP is not used: it only carries a free-text description."""
    facts = list(facts)
    seen: Dict[str, str] = {}
    for f in facts:
        for n in f.neighbors:
            if n.protocol == "cdp" and n.remote_platform:
                seen.setdefault(normalize_hostname(n.remote_name), n.remote_platform)
    for f in facts:
        if not f.model:
            f.model = seen.get(normalize_hostname(f.hostname), "")
