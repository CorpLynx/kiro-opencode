"""Configuration files: IOS / IOS-XE / NX-OS / EOS style ("interface X"
blocks) and Junos, in both `set` and curly-brace form.

A config says what is configured, not what is up, so interface status is
only ever "admin-down" (shutdown/disable) or unknown.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from ..facts import DeviceFacts, Interface
from .common import InterfaceTable, find_mgmt_ip, fold_junos_unit, to_cidr

_JUNOS_SET_RE = re.compile(r"^(set|deactivate) \S+", re.M)
_JUNOS_CURLY_RE = re.compile(r"^(system|interfaces|protocols|routing-instances|chassis|"
                             r"policy-options|security|groups)\s*\{|^version \S+;\s*$", re.M)
_IOS_RE = re.compile(r"^(hostname|switchname) \S+|^interface \S+", re.M)


def looks_like_config(text: str) -> bool:
    return bool(_JUNOS_SET_RE.search(text) and re.search(r"^set (system|interfaces) ", text, re.M)
                or _JUNOS_CURLY_RE.search(text) or _IOS_RE.search(text))


def is_junos_config(text: str) -> bool:
    return bool(re.search(r"^set (system|interfaces|protocols|routing-instances) ", text, re.M)
                or _JUNOS_CURLY_RE.search(text))


def parse_config(text: str, platform: Optional[str] = None, source: str = "") -> DeviceFacts:
    """Hostname and interfaces (addresses, VRF, description, shutdown) from
    a configuration. The hostname is "" if the config doesn't set one."""
    if platform == "juniper_junos" or (platform is None and is_junos_config(text)):
        hostname, interfaces = _parse_junos(text)
    else:
        hostname, interfaces = _parse_ios(text)
    facts = DeviceFacts(hostname, interfaces=interfaces, source=source)
    facts.mgmt_ip = find_mgmt_ip(interfaces)
    return facts


# -- IOS style ---------------------------------------------------------------------

def _parse_ios(text: str) -> Tuple[str, List[Interface]]:
    m = re.search(r"^(?:hostname|switchname)\s+(\S+)", text, re.M)
    hostname = m.group(1).strip('"') if m else ""
    table = InterfaceTable()
    current: Optional[Interface] = None
    for line in text.split("\n"):
        if not line.strip() or line.strip() == "!":
            if line.strip() == "!":
                current = None
            continue
        if not line[0].isspace():
            m = re.match(r"^interface\s+(.+?)\s*$", line)
            current = table.add(Interface(m.group(1))) if m else None
            continue
        if current is not None:
            _ios_interface_line(current, line.strip())
    return hostname, table.values()


def _ios_interface_line(iface: Interface, line: str) -> None:
    m = re.match(r"^description\s+(.*)$", line)
    if m:
        iface.description = m.group(1).strip().strip('"')
        return
    m = (re.match(r"^ip address\s+(\d+\.\d+\.\d+\.\d+)\s+(\d+\.\d+\.\d+\.\d+)", line)
         or re.match(r"^ip address\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", line))
    if m:
        _append(iface, to_cidr(m.group(1), m.group(2)))
        return
    m = re.match(r"^ipv6 address\s+([0-9A-Fa-f:]+/\d+)\s*$", line)
    if m:
        _append(iface, to_cidr(m.group(1)))
        return
    m = (re.match(r"^(?:ip )?vrf (?:forwarding|member)\s+(\S+)", line)
         or re.match(r"^vrf\s+(?!forwarding|member)(\S+)\s*$", line))
    if m:
        iface.vrf = m.group(1)
        return
    if line == "shutdown":
        iface.status = "admin-down"
    elif line == "no shutdown":
        iface.status = ""


def _append(iface: Interface, cidr) -> None:
    if cidr and cidr not in iface.addresses:
        iface.addresses.append(cidr)


# -- Junos ---------------------------------------------------------------------------

def junos_statements(text: str) -> List[List[str]]:
    """Both Junos forms as a list of `set` paths, so one reader handles
    them. Inactive statements ("inactive:" in braces, "deactivate" in set
    form) are dropped, since they are not in effect."""
    if re.search(r"^set \S+", text, re.M):
        return _set_statements(text)
    return _curly_statements(text)


def _words(line: str) -> List[str]:
    return [w[1:-1] if w.startswith('"') and w.endswith('"') and len(w) > 1 else w
            for w in re.findall(r'"(?:[^"\\]|\\.)*"|\S+', line)]


def _set_statements(text: str) -> List[List[str]]:
    statements, inactive = [], []
    for line in text.split("\n"):
        words = _words(line.strip())
        if len(words) > 1 and words[0] == "set":
            statements.append(words[1:])
        elif len(words) > 1 and words[0] == "deactivate":
            inactive.append(words[1:])
    return [s for s in statements if not any(s[:len(p)] == p for p in inactive)]


_TOKEN_RE = re.compile(r'"(?:[^"\\]|\\.)*"|/\*.*?\*/|(?:^|(?<=\s))#[^\n]*|[{};\[\]]|[^\s{};\[\]"]+',
                       re.S | re.M)


def _curly_statements(text: str) -> List[List[str]]:
    statements: List[List[str]] = []
    stack: List[Tuple[List[str], bool]] = []
    words: List[str] = []
    for tok in _TOKEN_RE.findall(text):
        if tok.startswith("/*") or tok.startswith("#") or tok in ("[", "]"):
            continue
        if tok == "{":
            stack.append(_strip_flags(words))
            words = []
        elif tok == "}":
            if stack:
                stack.pop()
            words = []
        elif tok == ";":
            path, inactive = _strip_flags(words)
            if path and not inactive and not any(flag for _, flag in stack):
                statements.append([w for group, _ in stack for w in group] + path)
            words = []
        else:
            words.append(tok[1:-1] if tok.startswith('"') else tok)
    return statements


def _strip_flags(words: List[str]) -> Tuple[List[str], bool]:
    inactive = bool(words) and words[0] == "inactive:"
    return [w for w in words if w not in ("inactive:", "protect:")], inactive


def _parse_junos(text: str) -> Tuple[str, List[Interface]]:
    hostname = ""
    table: Dict[str, Interface] = {}
    vrfs: Dict[str, str] = {}
    for s in junos_statements(text):
        if s[:2] == ["system", "host-name"] and len(s) > 2:
            hostname = hostname or s[2]
        elif s[0] == "interfaces" and len(s) > 2:
            _junos_interface(table, s[1], s[2:])
        elif s[0] == "routing-instances" and len(s) > 3 and s[2] == "interface":
            vrfs[fold_junos_unit(s[3])] = s[1]
    for name, vrf in vrfs.items():
        table.setdefault(name, Interface(name)).vrf = vrf
    return hostname, list(table.values())


def _junos_interface(table: Dict[str, Interface], ifd: str, rest: List[str]) -> None:
    if ifd in ("interface-range", "apply-groups", "apply-groups-except", "traceoptions") \
            or "<" in ifd or "*" in ifd:
        return
    name = ifd
    if rest[0] == "unit" and len(rest) > 1:
        name, rest = fold_junos_unit(f"{ifd}.{rest[1]}"), rest[2:]
    iface = table.setdefault(name, Interface(name))
    if not rest:
        return
    if rest[0] == "description" and len(rest) > 1:
        iface.description = iface.description or " ".join(rest[1:])
    elif rest[0] == "disable":
        iface.status = "admin-down"
    elif rest[0] == "family" and len(rest) > 3 and rest[1] in ("inet", "inet6") \
            and rest[2] == "address":
        _append(iface, to_cidr(rest[3]) if "/" in rest[3] else None)
