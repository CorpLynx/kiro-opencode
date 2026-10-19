"""Arista EOS output: `show version`, `show hostname` and `show lldp
neighbors detail`. EOS's `show ip interface` follows the IOS layout and is
read by the IOS parser."""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from ..facts import Neighbor
from .common import capabilities, is_ip, pick_port


def parse_version(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    patterns = {
        "model": r"^Arista (\S+)\s*$",
        "serial": r"^Serial number:\s*(\S+)",
        "os_version": r"^Software image version:\s*(\S+)",
        "hostname": r"^Hostname:\s*(\S+)",
    }
    for key, pattern in patterns.items():
        m = re.search(pattern, text, re.M)
        if m:
            out[key] = m.group(1)
    return out


def parse_hostname(text: str) -> Dict[str, str]:
    m = re.search(r"^Hostname:\s*(\S+)", text, re.M)
    return {"hostname": m.group(1)} if m else {}


def parse_lldp_detail(text: str) -> List[Neighbor]:
    """Sections "Interface Ethernet1 detected 1 LLDP neighbors:" hold one
    "Neighbor <chassis>/<port>" entry per neighbour, with "- Key: value"
    fields whose values are often quoted; the system description may run
    over several lines inside its quotes."""
    out: List[Neighbor] = []
    local: Optional[str] = None
    fields: Optional[Dict[str, str]] = None
    open_quote = False
    for line in text.split("\n"):
        if open_quote:
            open_quote = not line.rstrip().endswith('"')
            continue
        m = re.match(r"^Interface (\S+) detected \d+ LLDP neighbors?", line)
        if m:
            _flush(out, local, fields)
            local, fields = m.group(1), None
            continue
        if re.match(r"^\s+Neighbor \S+", line) and local:
            _flush(out, local, fields)
            fields = {}
            continue
        m = re.match(r"^\s*-?\s*([A-Za-z][A-Za-z ]*?)\s*:\s*(.*?)\s*$", line)
        if m and fields is not None:
            key, value = m.group(1).lower(), m.group(2)
            fields.setdefault(key, value.strip('"'))
            open_quote = value.startswith('"') and (len(value) == 1 or not value.endswith('"'))
    _flush(out, local, fields)
    return out


def _flush(out: List[Neighbor], local: Optional[str], fields: Optional[Dict[str, str]]) -> None:
    if not local or not fields:
        return
    name = fields.get("system name") or fields.get("chassis id", "")
    caps = fields.get("enabled capabilities") or fields.get("system capabilities", "")
    mgmt = fields.get("management address", "")
    out.append(Neighbor(local, name, pick_port(fields.get("port id", ""),
                                               fields.get("port description", "")),
                        mgmt if is_ip(mgmt) else None,
                        fields.get("system description", "").split("\n")[0],
                        capabilities(caps), "lldp"))
