"""Parsers for network device command output and configuration files.

Owned by the parsers workstream; see DESIGN.md. The live crawler
(collectors/devices.py) depends on exactly this interface:

* `COMMANDS[platform]`: the read-only commands to run on a device.
* `parse_outputs({command: output}, platform, source)`: facts from them.
* `parse_text(text, platform, source)`: facts from one saved file, a
  capture of several commands or a configuration.
* `detect_platform(text)`.

Parsing is best-effort: an unknown command, an error reply or a section
that doesn't parse is skipped, never fatal. Only `parse_text` /
`parse_file` raise (`ParseError`) when a file holds nothing recognisable.
"""

from __future__ import annotations

import re
from typing import Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from ..facts import DeviceFacts, normalize_hostname
from . import arista_eos, cisco_ios, cisco_nxos, juniper_junos, tables
from .capture import KNOWN_COMMANDS, Section, canonical_command, split_capture
from .common import (VENDORS, InterfaceTable, add_neighbor, clean_text, fill_from_neighbors,
                     find_mgmt_ip, hostname_from_source, is_error_output, merge_facts,
                     reconcile_ports)
from .config import looks_like_config, parse_config
from .detect import detect_platform

__all__ = ["PLATFORMS", "COMMANDS", "KNOWN_COMMANDS", "ParseError", "parse_outputs", "parse_text",
           "parse_file", "detect_platform", "canonical_command", "split_capture", "merge_facts",
           "fill_from_neighbors", "parse_config"]

PLATFORMS = ("cisco_ios", "cisco_nxos", "arista_eos", "juniper_junos")

# The read-only commands the live crawler runs on each platform, in order.
# Every command here must be understood by parse_outputs(). EOS's
# `show version` has no hostname, hence `show hostname`. Junos has no
# `show lldp neighbors detail`; its summary gives ports and names but no
# management addresses. Saved captures may also hold interface
# descriptions, `show ip interface brief` and configs, which parse too.
COMMANDS: Dict[str, list] = {
    "cisco_ios": ["show version", "show cdp neighbors detail", "show lldp neighbors detail",
                  "show ip interface", "show ipv6 interface"],
    "cisco_nxos": ["show version", "show cdp neighbors detail", "show lldp neighbors detail",
                   "show ip interface vrf all", "show ipv6 interface vrf all"],
    "arista_eos": ["show version", "show hostname", "show lldp neighbors detail",
                   "show ip interface", "show ipv6 interface"],
    "juniper_junos": ["show version", "show chassis hardware", "show lldp neighbors",
                      "show interfaces terse"],
}


class ParseError(ValueError):
    """A file held no recognisable command output or configuration."""


# -- per-kind dispatch ---------------------------------------------------------------
# Each kind picks its parser from the output's layout, falling back to the
# platform, so a mislabelled or undetected platform still parses.

def _version(text: str, platform: Optional[str]) -> Dict[str, str]:
    if re.search(r"Cisco Nexus Operating System|^\s+NXOS: version|^\s+Device name:", text, re.M):
        return cisco_nxos.parse_version(text)
    if re.search(r"^Software image version:|^Arista ", text, re.M):
        return arista_eos.parse_version(text)
    if re.search(r"^Junos:|^JUNOS |^Model: ", text, re.M):
        return juniper_junos.parse_version(text)
    return cisco_ios.parse_version(text)


def _lldp(text: str, platform: Optional[str]):
    if re.search(r"^Interface \S+ detected \d+ LLDP", text, re.M):
        return arista_eos.parse_lldp_detail(text)
    if re.search(r"^Local Port id:", text, re.M):
        return cisco_nxos.parse_lldp_detail(text)
    if re.search(r"^Local Intf:", text, re.M):
        return cisco_ios.parse_lldp_detail(text)
    if re.search(r"^Local Interface", text, re.M):
        return juniper_junos.parse_lldp(text)
    return []


def _ip(text: str, platform: Optional[str]):
    if re.search(r"^IP(?:v6)? Interface Status for VRF", text, re.M) or \
            re.search(r"^\S+, Interface status:", text, re.M):
        return cisco_nxos.parse_ip_interface(text)
    return cisco_ios.parse_ip_interface(text)


_KIND_PARSERS: Dict[str, Callable] = {
    "version": _version,
    "hostname": lambda text, p: arista_eos.parse_hostname(text),
    "inventory": lambda text, p: juniper_junos.parse_chassis_hardware(text),
    "cdp": lambda text, p: cisco_ios.parse_cdp_detail(text),
    "lldp": _lldp,
    "ip": _ip,
    "ip-brief": lambda text, p: tables.parse_ip_brief(text),
    "terse": lambda text, p: juniper_junos.parse_terse(text),
    "descriptions": lambda text, p: tables.parse_descriptions(text),
    "config": lambda text, p: parse_config(text, p),
}
# Runtime state first: the first non-empty value of a field wins.
_ORDER = ["version", "hostname", "inventory", "ip", "terse", "ip-brief", "descriptions", "cdp",
          "lldp", "config"]

# Layouts that identify a kind of output when a file has no prompts.
_KIND_SIGNATURES = {
    "version": r"^(?!Kernel )\S+ uptime is |^Cisco Nexus Operating System|^Software image version:"
               r"|^Junos: |^Hostname: \S+\s*\nModel:",
    "inventory": r"^Hardware inventory:",
    "cdp": r"^Interface:.*Port ID \(outgoing port\)",
    "lldp": r"^Local Intf:|^Local Port id:|^Interface \S+ detected \d+ LLDP|"
            r"^LLDP Neighbor Information:|^Local Interface\s+Parent Interface",
    "ip": r"^\S+ is (?:up|down|administratively down), line protocol|^IP(?:v6)? Interface Status for VRF",
    "terse": r"^Interface\s+Admin\s+Link\s+Proto",
    "descriptions": r"^(?:Interface|Port)\s+(?:Status|Type|Admin)\s+.*Description\s*$",
    "ip-brief": r"^Interface\s+IP[- ]Address\s+(?:OK\?|Status|Interface Status)",
}


def _parse_sections(sections: Sequence[Tuple[str, str]], platform: Optional[str], source: str,
                    hostname: str = "", kinds: Optional[List[str]] = None) -> DeviceFacts:
    """Facts from (command, output) pairs. `kinds`, if given, says what each
    output is instead of looking the command up."""
    pairs = []
    for i, (command, output) in enumerate(sections):
        output = clean_text(output)
        kind = kinds[i] if kinds else KNOWN_COMMANDS.get(canonical_command(command) or "")
        if kind and output.strip() and not is_error_output(output):
            pairs.append((kind, output))
    pairs.sort(key=lambda kv: _ORDER.index(kv[0]))
    platform = platform or detect_platform("\n".join(out for _, out in pairs))

    found: Dict[str, str] = {}
    table = InterfaceTable()
    facts = DeviceFacts("", platform=platform or "", vendor=VENDORS.get(platform or "", ""),
                        source=source)
    configs: List[DeviceFacts] = []
    for kind, output in pairs:
        try:
            result = _KIND_PARSERS[kind](output, platform)
        except (ValueError, IndexError, KeyError):
            continue
        if isinstance(result, dict):
            for key, value in result.items():
                found.setdefault(key, value)
        elif isinstance(result, DeviceFacts):
            configs.append(result)
        elif kind in ("cdp", "lldp"):
            for n in result:
                add_neighbor(facts.neighbors, n)
        else:
            table.extend(result)
    for cfg in configs:
        table.extend(cfg.interfaces)
        found.setdefault("hostname", cfg.hostname)

    facts.hostname = (found.get("hostname") or hostname
                      or (hostname_from_source(source) if source else "unknown"))
    facts.model = found.get("model", "")
    facts.os_version = found.get("os_version", "")
    facts.serial = found.get("serial", "")
    facts.interfaces = table.values()
    facts.mgmt_ip = find_mgmt_ip(facts.interfaces)
    reconcile_ports(facts.neighbors)
    return facts


def parse_outputs(outputs: Mapping[str, str], platform: Optional[str] = None,
                  source: str = "", hostname: str = "") -> DeviceFacts:
    """Facts from command outputs, keyed by the command that produced them
    (full or abbreviated). Unknown commands and error replies are ignored.
    The hostname comes from the outputs, else `hostname` (e.g. the
    prompt), else the source name."""
    return _parse_sections(list(outputs.items()), platform, source, hostname)


def parse_file(text: str, platform: Optional[str] = None, source: str = "") -> List[DeviceFacts]:
    """Facts for every device in one saved file. A capture whose prompts
    name several hosts yields one DeviceFacts per host; a configuration or
    a single output without prompts yields one. Raises ParseError if
    nothing in the file is recognisable."""
    sections = split_capture(text)
    if sections:
        return _parse_capture(sections, platform, source)
    text = clean_text(text)
    kinds = [kind for kind, sig in _KIND_SIGNATURES.items() if re.search(sig, text, re.M)]
    if looks_like_config(text):
        kinds.append("config")
    if not kinds:
        raise ParseError("no recognisable command output or configuration")
    return [_parse_sections([("", text)] * len(kinds), platform, source, kinds=kinds)]


def _parse_capture(sections: List[Section], platform: Optional[str],
                   source: str) -> List[DeviceFacts]:
    groups: Dict[str, List[Section]] = {}
    host = ""
    for s in sections:
        host = s.host or host
        groups.setdefault(normalize_hostname(host) if host else "", []).append(s)
    if "" in groups and len(groups) > 1:   # banner sections before the first prompt
        first = next(k for k in groups if k)
        groups[first] = groups.pop("") + groups[first]
    out = []
    for key, group in groups.items():
        prompt_host = next((s.host for s in group if s.host), "")
        out.append(_parse_sections([(s.command, s.output) for s in group], platform, source,
                                   prompt_host))
    if not any(f.interfaces or f.neighbors or f.model or f.os_version for f in out) \
            and not any(s.host for s in sections):
        raise ParseError("no recognisable command output")
    return out


def parse_text(text: str, platform: Optional[str] = None, source: str = "") -> DeviceFacts:
    """Facts from one saved file: a capture of several commands (each after a
    prompt line like "core1#show version" or a banner), or a configuration.
    If the file holds several devices, the first is returned; `parse_file`
    returns them all. Raises ParseError if nothing is recognisable."""
    return parse_file(text, platform, source)[0]

