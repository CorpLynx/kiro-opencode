"""Write the plan's new ranges as an Infoblox (NIOS) CSV import file.

Each object type is a section with its own `header-...` row, as Grid
Manager's CSV import expects, and containers come before the networks that
go inside them, widest first:

    header-networkcontainer,address*,netmask*,network_view,comment,EA-Owner
    networkcontainer,10.0.0.0,8,default,IPAM top pool,platform
    header-network,address*,netmask*,network_view,comment,EA-Owner
    network,10.1.0.0,255.255.255.0,default,app-a (subnet-0a1b),platform
    header-ipv6network,address*,cidr*,network_view,comment,EA-Owner

IPv4 networks take a dotted netmask and IPv4 containers a prefix length,
as the NIOS CSV Import Reference documents them; `--netmask-style` forces
one form for both if your NIOS version wants otherwise. IPv6 objects always
take the prefix length in `cidr*`.
"""

from __future__ import annotations

import csv
import io
import os
from typing import Dict, List, Sequence, Tuple

from .plan import Entry, Options, Plan, comment_for, ea_values

NETMASK_STYLES = ("documented", "dotted", "prefix")
_SECTIONS = (  # (IP version, kind, object keyword, mask column)
    (4, "container", "networkcontainer", "netmask*"),
    (4, "network", "network", "netmask*"),
    (6, "container", "ipv6networkcontainer", "cidr*"),
    (6, "network", "ipv6network", "cidr*"),
)


def _mask(entry: Entry, style: str) -> str:
    cidr = entry.cidr
    if cidr.version == 6:
        return str(cidr.prefixlen)
    dotted = style == "dotted" or (style == "documented" and entry.kind == "network")
    return str(cidr.netmask) if dotted else str(cidr.prefixlen)


def sections(plan: Plan, options: Options, style: str = "documented",
             kinds: Sequence[str] = ("container", "network")) -> List[List[List[str]]]:
    """Header-plus-rows blocks for the new entries, in import order."""
    if style not in NETMASK_STYLES:
        raise ValueError(f"--netmask-style must be one of {', '.join(NETMASK_STYLES)}")
    ea_names = [name for name, _ in options.eas]
    new = plan.by_status("new")
    blocks = []
    for version, kind, keyword, mask_column in _SECTIONS:
        if kind not in kinds:
            continue
        members = [e for e in new if e.cidr.version == version and e.kind == kind]
        if not members:
            continue
        if kind == "container":  # parents before the containers inside them
            members.sort(key=lambda e: (e.cidr.prefixlen, int(e.cidr.network_address), e.view))
        block = [[f"header-{keyword}", "address*", mask_column, "network_view", "comment"]
                 + [f"EA-{n}" for n in ea_names]]
        for entry in members:
            values = ea_values(entry, options)
            block.append([keyword, str(entry.cidr.network_address), _mask(entry, style), entry.view,
                          comment_for(entry, options)] + [values.get(n, "") for n in ea_names])
        blocks.append(block)
    return blocks


def render(blocks: Sequence[Sequence[Sequence[str]]]) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    for block in blocks:
        writer.writerows(block)
    return out.getvalue()


def write_private(path: str, text: str) -> None:
    """Real address plans are sensitive: the file is readable by its owner only."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def write_import(plan: Plan, options: Options, path: str, style: str = "documented",
                 split: bool = False) -> List[Tuple[str, int]]:
    """Write the import file(s); returns (path, objects) for each one written.
    With `split`, containers and networks go to separate files
    (`name-containers.csv`, `name-networks.csv`), for NIOS versions that
    won't import containers or for importing in two steps."""
    targets: List[Tuple[str, Sequence[str]]] = [(path, ("container", "network"))]
    if split:
        stem, ext = os.path.splitext(path)
        targets = [(f"{stem}-containers{ext or '.csv'}", ("container",)),
                   (f"{stem}-networks{ext or '.csv'}", ("network",))]
    written = []
    for target, kinds in targets:
        blocks = sections(plan, options, style, kinds)
        write_private(target, render(blocks))
        written.append((target, sum(len(b) - 1 for b in blocks)))
    return written


def counts(plan: Plan) -> Dict[str, int]:
    new = plan.by_status("new")
    return {"containers": sum(1 for e in new if e.kind == "container"),
            "networks": sum(1 for e in new if e.kind == "network"),
            "exists": len(plan.by_status("exists")),
            "conflicts": len(plan.by_status("conflict"))}
