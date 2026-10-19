"""Column-aligned tables that every vendor prints in roughly the same way:
interface descriptions and `show ip interface brief`."""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from ..facts import Interface
from .common import fold_junos_unit, to_cidr

_DESC_HEADER_RE = re.compile(r"^(Interface|Port)\s+.*\bDescription\s*$")


def parse_descriptions(text: str) -> List[Interface]:
    """IOS/EOS `show interfaces description` (Interface Status Protocol
    Description), NX-OS `show interface description` (Port Type Speed
    Description, then a second Interface/Description table) and Junos
    `show interfaces descriptions` (Interface Admin Link Description).
    Columns are found from the header, because descriptions and IOS's
    "admin down" status contain spaces."""
    out: List[Interface] = []
    cols: Optional[Dict[str, int]] = None
    for line in text.split("\n"):
        if _DESC_HEADER_RE.match(line):
            cols = {m.group(0).lower(): m.start() for m in re.finditer(r"\S+", line)}
            continue
        if cols is None or not line.strip() or re.match(r"^[-=\s]+$", line):
            continue
        name = line.split()[0]
        desc = line[cols["description"]:].strip() if len(line) > cols["description"] else ""
        desc = "" if desc.strip("-") == "" else desc
        out.append(Interface(fold_junos_unit(name), description=desc,
                             status=_table_status(line, cols)))
    return out


def _cell(line: str, cols: Dict[str, int], key: str) -> str:
    start = cols[key]
    later = [p for p in cols.values() if p > start]
    return line[start:min(later) if later else len(line)].strip().lower()


def _table_status(line: str, cols: Dict[str, int]) -> str:
    if "status" in cols and "protocol" in cols:
        admin, link = _cell(line, cols, "status"), _cell(line, cols, "protocol")
    elif "admin" in cols and "link" in cols:
        admin, link = _cell(line, cols, "admin"), _cell(line, cols, "link")
    else:
        return ""
    if "admin" in admin or admin in ("down", "disabled") and "admin" in cols:
        return "admin-down"
    return "up" if admin == "up" and link.startswith("up") else "down"


def parse_ip_brief(text: str) -> List[Interface]:
    """`show ip interface brief`. IOS and NX-OS print no mask, so their
    addresses are dropped (an address without a prefix can't be placed in a
    subnet; the masked form comes from `show ip interface` or the config)
    and the table contributes interface names and status only. EOS prints
    "10.0.0.9/31", which is kept."""
    out: List[Interface] = []
    vrf, fmt = "", ""
    for line in text.split("\n"):
        m = re.match(r'^IP Interface Status for VRF "([^"]+)"', line)
        if m:
            vrf = "" if m.group(1) == "default" else m.group(1)
            continue
        if re.match(r"^Interface\s+IP[- ]Address", line):
            fmt = "ios" if "OK?" in line else "eos" if "Protocol" in line else "nxos"
            continue
        tokens = line.split()
        if not fmt or len(tokens) < 3 or re.match(r"^[-\s]+$", line):
            continue
        iface = Interface(tokens[0], vrf=vrf)
        cidr = to_cidr(tokens[1]) if "/" in tokens[1] else None
        if cidr:
            iface.addresses.append(cidr)
        if fmt == "ios":
            admin, proto = " ".join(tokens[4:-1]).lower(), tokens[-1].lower()
        elif fmt == "eos":
            admin, proto = tokens[2].lower(), tokens[3].lower() if len(tokens) > 3 else ""
        else:
            admin = proto = tokens[2].lower()
        if admin.startswith("admin") or "admin-down" in admin:
            iface.status = "admin-down"
        elif admin.split("/")[0] in ("up", "protocol-up") and proto.split("/")[0] in ("up", "protocol-up"):
            iface.status = "up"
        else:
            iface.status = "down"
        out.append(iface)
    return out
