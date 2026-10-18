"""Split a saved terminal capture into (command, output) sections, and
expand abbreviated commands ("sh cdp nei det") to their full form."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Set, Tuple

from .common import clean_text

# Every command a parser understands, mapped to the kind of output it gives.
KNOWN_COMMANDS = {
    "show version": "version",
    "show hostname": "hostname",
    "show chassis hardware": "inventory",
    "show cdp neighbors detail": "cdp",
    "show lldp neighbors detail": "lldp",
    "show lldp neighbors": "lldp",
    "show ip interface": "ip",
    "show ip interface vrf all": "ip",
    "show ipv6 interface": "ip",
    "show ipv6 interface vrf all": "ip",
    "show ip interface brief": "ip-brief",
    "show ip interface brief vrf all": "ip-brief",
    "show interfaces terse": "terse",
    "show interfaces description": "descriptions",
    "show interface description": "descriptions",
    "show interfaces descriptions": "descriptions",
    "show running-config": "config",
    "show startup-config": "config",
    "show configuration": "config",
}


def canonical_command(command: str) -> Optional[str]:
    """The known command that `command` abbreviates, or None.

    Words match by prefix as on the CLI ("sh ip int br"), an exact word
    beats a prefix ("ip" is "ip", not "ipv6"), and a pipe ("| no-more")
    is ignored. Extra trailing words ("show lldp neighbors interface
    xe-0/1/0", "show running-config all") fall back to the longest known
    command they start with.
    """
    words = command.split("|")[0].lower().split()
    if not words:
        return None
    best: List[Tuple[int, str]] = []
    for known in KNOWN_COMMANDS:
        kw = known.split()
        if len(kw) == len(words) and all(k.startswith(w) for w, k in zip(words, kw)):
            best.append((sum(w == k for w, k in zip(words, kw)), known))
    if best:
        top = max(score for score, _ in best)
        winners = [k for score, k in best if score == top]
        if len({KNOWN_COMMANDS[k] for k in winners}) == 1:
            return winners[0]
        return None
    longer = [k for k in KNOWN_COMMANDS
              if len(k.split()) < len(words) and all(kw.startswith(w) or w == kw
                                                     for w, kw in zip(words, k.split()))
              and all(len(w) >= 2 or i == 0 for i, w in enumerate(words[:len(k.split())]))]
    return max(longer, key=lambda k: len(k.split())) if longer else None


def is_show_command(command: str) -> bool:
    words = command.strip().lower().split()
    return bool(words) and len(words[0]) >= 2 and "show".startswith(words[0])


@dataclass
class Section:
    command: str              # canonical command, or the text as typed if unknown
    output: str
    host: str = ""            # hostname from the prompt or banner, "" if none
    lines: List[str] = field(default_factory=list, repr=False)


_PROMPT_RE = re.compile(r"^(?:[\w.-]+@)?(?P<host>[A-Za-z0-9][\w.-]*?)(?:\([\w.-]+\))?[#>]\s?(?P<cmd>.*)$")
_BANNER_RES = [
    re.compile(r"^={3,}\s*(?:(?P<host>[\w.-]+)\s*[:#>]\s*)?(?P<cmd>.+?)\s*={3,}\s*$"),
    re.compile(r"^-{3,}\s*(?:(?P<host>[\w.-]+)\s*[:#>]\s*)?(?P<cmd>.+?)\s*-{3,}\s*$"),
    re.compile(r"^\*{3,}\s*(?:(?P<host>[\w.-]+)\s*[:#>]\s*)?(?P<cmd>.+?)\s*\*{3,}\s*$"),
    re.compile(r"^#+\s*(?P<cmd>.+?)\s*#*\s*$"),
    re.compile(r"^!+\s*(?P<cmd>.+?)\s*!*\s*$"),
]


def split_capture(text: str) -> List[Section]:
    """Sections of a capture, each starting at a prompt line ("core1#show
    version", "core1>sh ver", "user@edge1> show lldp neighbors") or a
    banner ("===== show version =====", "! show version", "### show
    version"). Text before the first section is ignored.

    To avoid mistaking output for a prompt, a prompt for a host not seen
    yet must carry a show command; after that, any line with that host's
    prompt starts a section (so "terminal length 0" or a bare prompt ends
    the previous output). Banners must carry a show command.
    """
    sections: List[Section] = []
    hosts: Set[str] = set()
    for line in clean_text(text).split("\n"):
        start = _section_start(line, hosts)
        if start is None:
            if sections:
                sections[-1].lines.append(line)
            continue
        host, cmd = start
        if host:
            hosts.add(host.lower())
        sections.append(Section(canonical_command(cmd) or cmd.strip(), "", host))
    for s in sections:
        s.output = "\n".join(s.lines).strip("\n")
        s.lines = []
    return sections


def _section_start(line: str, hosts: Set[str]) -> Optional[Tuple[str, str]]:
    m = _PROMPT_RE.match(line)
    if m and (m.group("host").lower() in hosts or is_show_command(m.group("cmd"))):
        return m.group("host"), m.group("cmd").strip()
    for regex in _BANNER_RES:
        m = regex.match(line)
        if m and is_show_command(m.group("cmd")):
            return (m.groupdict().get("host") or ""), m.group("cmd").strip()
    return None
