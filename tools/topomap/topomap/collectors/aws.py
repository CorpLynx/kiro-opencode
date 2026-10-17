"""Collect VPCs, subnets, gateways, TGWs, VPNs, peering and Direct Connect from AWS (read-only).

Owned by the AWS collector workstream; see DESIGN.md.
"""

from __future__ import annotations

HELP = "Collect VPCs, subnets, gateways, TGWs, VPNs, peering and Direct Connect from AWS (read-only)."
PRODUCES_TOPOLOGY = True


def add_arguments(parser) -> None:
    pass


def run(args):
    raise RuntimeError("not implemented yet")
