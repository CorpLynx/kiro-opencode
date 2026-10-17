"""Parsers for network device command output and configuration files.

Owned by the parsers workstream; see DESIGN.md. The live crawler
(collectors/devices.py) depends on exactly this interface.
"""

from __future__ import annotations

from typing import Dict, Mapping, Optional

from ..facts import DeviceFacts

PLATFORMS = ("cisco_ios", "cisco_nxos", "arista_eos", "juniper_junos")

# The read-only commands the live crawler runs on each platform, in order.
# Every command here must be understood by parse_outputs().
COMMANDS: Dict[str, list] = {
    "cisco_ios": ["show version", "show cdp neighbors detail", "show lldp neighbors detail",
                  "show ip interface", "show ipv6 interface"],
    "cisco_nxos": ["show version", "show cdp neighbors detail", "show lldp neighbors detail",
                   "show ip interface vrf all", "show ipv6 interface vrf all"],
    "arista_eos": ["show version", "show lldp neighbors detail", "show ip interface",
                   "show ipv6 interface"],
    "juniper_junos": ["show version", "show lldp neighbors detail", "show interfaces terse"],
}


def parse_outputs(outputs: Mapping[str, str], platform: Optional[str] = None,
                  source: str = "") -> DeviceFacts:
    """Facts from command outputs, keyed by the command that produced them."""
    raise NotImplementedError


def parse_text(text: str, platform: Optional[str] = None, source: str = "") -> DeviceFacts:
    """Facts from one saved file: a capture of several commands (each after a
    prompt line like "core1#show version" or a banner), or a configuration."""
    raise NotImplementedError


def detect_platform(text: str) -> Optional[str]:
    """Guess the platform from command output or a configuration."""
    raise NotImplementedError
