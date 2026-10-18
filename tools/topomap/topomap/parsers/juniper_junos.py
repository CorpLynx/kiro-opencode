"""Juniper Junos output: `show version`, `show chassis hardware`,
`show lldp neighbors` (the summary table, and the detailed blocks printed
by `show lldp neighbors interface X`) and `show interfaces terse`.

Junos puts addresses on logical units ("xe-0/1/0.0") while LLDP names the
port; unit 0 is folded into the port name (see `fold_junos_unit`)."""

from __future__ import annotations

import ipaddress
import re
from typing import Dict, List, Optional

from ..facts import Interface, Neighbor
from .common import capabilities, fold_junos_unit, is_ip, pick_port, to_cidr


def parse_version(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for key, pattern in (("hostname", r"^Hostname:\s*(\S+)"), ("model", r"^Model:\s*(\S+)"),
                         ("os_version", r"^Junos:\s*(\S+)"),
                         ("os_version", r"^JUNOS Software Release \[(\S+?)\]"),
                         ("os_version", r"^JUNOS (?:Base )?OS (?:boot|Kernel)[^\[]*\[(\S+?)\]")):
        m = re.search(pattern, text, re.M)
        if m and key not in out:
            out[key] = m.group(1)
    return out


def parse_chassis_hardware(text: str) -> Dict[str, str]:
    """The "Chassis" row: its Version and Part number cells are blank, so
    the first value is the serial and the rest the description (model)."""
    m = re.search(r"^Chassis\s+(\S+)(?:\s+(\S.*?))?\s*$", text, re.M)
    if not m:
        return {}
    out = {"serial": m.group(1)}
    if m.group(2):
        out["model"] = m.group(2)
    return out


# -- LLDP -----------------------------------------------------------------------

def parse_lldp(text: str) -> List[Neighbor]:
    """Detailed blocks if present, otherwise the summary table."""
    if re.search(r"^Local Interface\s*:", text, re.M):
        return _parse_lldp_blocks(text)
    return _parse_lldp_summary(text)


def _parse_lldp_blocks(text: str) -> List[Neighbor]:
    out = []
    for block in re.split(r"^(?=LLDP Neighbor Information:|Local Information:)", text, flags=re.M):
        fields: Dict[str, str] = {}
        mgmt: Optional[str] = None
        section = ""
        for line in block.split("\n"):
            if re.match(r"^(Management Info|System capabilities)", line, re.I):
                section = line.strip().lower()
                continue
            m = re.match(r"^(\s*)([A-Za-z][\w ]*?)\s*:\s*(.*?)\s*$", line)
            if not m:
                continue
            indented, key, value = bool(m.group(1)), m.group(2).lower(), m.group(3)
            if indented and section.startswith("management") and key == "address" and is_ip(value):
                mgmt = mgmt or value
            elif indented and section.startswith("system capabilities"):
                fields.setdefault("caps " + key, value)
            elif not indented:
                section = ""
                fields.setdefault(key, value)
        local = fields.get("local interface")
        if not local:
            continue
        name = fields.get("system name") or fields.get("chassis id", "")
        caps = fields.get("caps enabled") or fields.get("caps supported", "")
        out.append(Neighbor(fold_junos_unit(local), name,
                            _strip_unit(pick_port(fields.get("port id", ""),
                                                  fields.get("port description", ""))),
                            mgmt, fields.get("system description", ""), capabilities(caps), "lldp"))
    return out


def _parse_lldp_summary(text: str) -> List[Neighbor]:
    """Columns are taken from the header: "Port info" can hold a free-text
    port description with spaces."""
    out: List[Neighbor] = []
    cols: Optional[List[tuple]] = None
    for line in text.split("\n"):
        if re.match(r"^Local Interface\s+", line) and "System Name" in line:
            heads = [m for m in re.finditer(r"\S+(?: \S+)*", line)]
            cols = [(m.group(0).lower(), m.start()) for m in heads]
            continue
        if not cols or not line.strip():
            continue
        cells = {}
        for i, (name, start) in enumerate(cols):
            end = cols[i + 1][1] if i + 1 < len(cols) else len(line)
            cells[name] = line[start:end].strip()
        local, remote = cells.get("local interface", ""), cells.get("system name", "")
        if local and remote:
            out.append(Neighbor(fold_junos_unit(local.split()[0]), remote,
                                _strip_unit(cells.get("port info", "")), protocol="lldp"))
    return out


def _strip_unit(port: str) -> str:
    """A Junos neighbour may report "ge-0/0/0.0"; the cable is on ge-0/0/0."""
    return fold_junos_unit(port) if re.match(r"^[a-z]{2,4}-\d+/\d+/\d+\.0$", port) else port


# -- show interfaces terse ---------------------------------------------------------

_PORT_RE = re.compile(r"^(?:ge|xe|et|mge|fe|ae|reth|fab|fxp|em|me|vme|irb|vlan|lo|gr|st|lt|ip)"
                      r"(?:-?\d|$|\.)")
_INTERNAL = ipaddress.ip_network("128.0.0.0/2")


def parse_terse(text: str) -> List[Interface]:
    """Rows are "name admin link [proto local [remote]]"; further addresses
    continue on indented lines. Internal plumbing is dropped: interfaces
    other than ports, loopback, IRB and tunnels, the lo0.16384-style
    internal units, and anything carrying the RE-to-PFE 128.0.0.0/2
    network."""
    out: Dict[str, Interface] = {}
    internal = set()
    current: Optional[Interface] = None
    for line in text.split("\n"):
        tokens = line.split()
        if not tokens or tokens[0] == "Interface":
            continue
        if not line[0].isspace():
            name = tokens[0]
            if not _PORT_RE.match(name) or re.search(r"\.(1638\d|32767)$", name):
                current = None
                continue
            current = out.setdefault(fold_junos_unit(name), Interface(fold_junos_unit(name)))
            if len(tokens) >= 3 and not current.status:
                current.status = ("admin-down" if tokens[1] == "down"
                                  else "up" if tokens[2] == "up" else "down")
            tokens = tokens[3:]
        if current is None:
            continue
        if tokens and tokens[0] in ("inet", "inet6", "iso", "mpls", "eth-switch", "multiservice",
                                    "vpls", "bridge", "aenet", "tnp", "ccc"):
            family, tokens = tokens[0], tokens[1:]
        else:
            family = "inet6" if tokens and ":" in tokens[0] else "inet"
        if not tokens or family not in ("inet", "inet6"):
            continue
        cidr = to_cidr(tokens[0]) if "/" in tokens[0] else to_cidr(
            tokens[0], "32" if family == "inet" else "128")
        if not cidr:
            continue
        ip = ipaddress.ip_interface(cidr)
        if ip.version == 4 and ip.network.prefixlen == 2 and ip.ip in _INTERNAL:
            internal.add(current.name)
        elif not (ip.ip.is_loopback or ip.ip.is_link_local) and cidr not in current.addresses:
            current.addresses.append(cidr)
    return [i for name, i in out.items() if name not in internal]
