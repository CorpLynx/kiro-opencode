"""Guess the platform from text.

Captures quote other vendors: a Cisco switch's CDP output contains its
Nexus neighbour's version string, and LLDP carries every neighbour's
system description. So the signatures below are lines only the device's
*own* output or config prints (`show version` headers, config syntax,
prompt shapes, per-platform output layouts), and the platform with the
most matching signatures wins.
"""

from __future__ import annotations

import re
from typing import Optional

_SIGNATURES = {
    "juniper_junos": [
        r"^Junos: \S+", r"^JUNOS (?:OS|Base OS|Software Release)", r"^Model: [a-z]+\d",
        r"^set system host-name ", r"^\s*host-name \S+;", r"^## Last (?:commit|changed): ",
        r"^version \S+;\s*$", r"^Interface\s+Admin\s+Link\s+Proto", r"^LLDP Neighbor Information:",
        r"^Local Interface\s+Parent Interface", r"^Hardware inventory:",
        r"^[\w.-]+@[\w.-]+> ", r"^Interface\s+Admin\s+Link\s+Description",
    ],
    "cisco_nxos": [
        r"^Cisco Nexus Operating System \(NX-OS\) Software\s*$", r"^\s+NXOS: version ",
        r"^\s+(?:system|kickstart):\s+version ", r"^\s+Device name: ", r"^!Command: show running-config",
        r"^feature \S+", r"^IPv?6? ?Interface Status for VRF", r"^Local Port id: ", r"^switchname ",
        r"^\s+vrf member \S+", r"^version \d+\.\d+\(\d+\)", r"^Port\s+Type\s+Speed\s+Description",
    ],
    "arista_eos": [
        r"^Arista [A-Za-z]+-\S+\s*$", r"^Software image version: ", r"^! device: \S+ \(.*EOS-",
        r"^Interface \S+ detected \d+ LLDP neighbors?:", r"^Hostname: \S+\s*\n\s*FQDN: ",
        r"^Architecture: ", r"^management api http-commands", r"^\s+vrf [A-Za-z][\w-]*\s*$",
    ],
    "cisco_ios": [
        r"^Cisco IOS XE Software, Version ", r"^(?!Kernel )\S+ uptime is \d", r"^Local Intf: ",
        r"^Device ID: ", r"^Building configuration\.\.\.", r"^boot-start-marker",
        r"^version 1[2-9]\.\d+\s*$", r"^Interface\s+IP-Address\s+OK\?", r"^\s+vrf forwarding \S+",
        r"^\s+ip vrf forwarding \S+", r"^Processor board ID ",
    ],
}
_COMPILED = {p: [re.compile(s, re.M) for s in sigs] for p, sigs in _SIGNATURES.items()}


def detect_platform(text: str) -> Optional[str]:
    """The platform whose signatures match most often, or None."""
    scores = {p: sum(1 for r in regexes if r.search(text)) for p, regexes in _COMPILED.items()}
    best = max(scores, key=lambda p: scores[p])
    return best if scores[best] else None
