"""Collect Google Cloud networking: VPC networks, subnets, Cloud Routers,
Cloud VPN, Interconnect, VPC peering, Shared VPC and Network Connectivity
Center (read-only).

Which projects: `--project` names them, `--folder` and `--organization`
walk the hierarchy and take every project below. Each project is collected
into its own small topology on a thread pool and the pieces are merged in
sorted order, so the same estate always produces the same JSON.

A VPC network is global, so the picture is project > network > region >
subnet, with Cloud Routers and VPN gateways in the region they serve.
External VPN gateways (the on-prem end of a VPN, as GCP records it) and
Interconnects sit in their project; merge stitches on-prem devices to them
by address, like AWS customer gateways and Direct Connect VIFs.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from ..model import (GCP_INSTANCE, GCP_INTERCONNECT, GCP_NCC_HUB, GCP_NETWORK, GCP_PEER_GATEWAY,
                     GCP_PROJECT, GCP_REGION, GCP_ROUTER, GCP_SUBNET, GCP_VPN_GATEWAY,
                     LINK_INTERCONNECT_ATTACHMENT, LINK_NCC_SPOKE, LINK_SHARED_VPC,
                     LINK_VPC_PEERING, LINK_VPN, NET_EXTERNAL, Topology, node_id, union)
from .gcp_api import (COMPUTE, NCC, Api, ApiError, add_auth_arguments, api_from_args, link_project,
                      link_region, short, walk)

HELP = ("Collect GCP VPC networks, subnets, Cloud Routers, VPN, Interconnect, peering, Shared VPC "
        "and NCC (read-only).")
PRODUCES_TOPOLOGY = True

EXTRAS = ("instances",)

_PLACEHOLDER_KINDS = {"project": GCP_PROJECT, "network": GCP_NETWORK, "router": GCP_ROUTER,
                      "vpn-gateway": GCP_VPN_GATEWAY, "external-vpn-gateway": GCP_PEER_GATEWAY,
                      "interconnect": GCP_INTERCONNECT}


def _include_list(value: str) -> List[str]:
    items = [v.strip() for v in value.split(",") if v.strip()]
    unknown = [v for v in items if v not in EXTRAS]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown --include value {', '.join(unknown)} (choose from {', '.join(EXTRAS)})")
    return items


def add_arguments(parser) -> None:
    parser.add_argument("--project", action="append", default=[], metavar="ID",
                        help="project to collect (repeatable)")
    parser.add_argument("--folder", action="append", default=[], metavar="ID",
                        help="collect every project below this folder (repeatable)")
    parser.add_argument("--organization", metavar="ID",
                        help="collect every project in the organization")
    parser.add_argument("--include", action="extend", type=_include_list, default=[],
                        metavar="LIST", help="comma-separated extras: instances (Compute Engine VMs)")
    parser.add_argument("--workers", type=int, default=8, metavar="N",
                        help="projects collected in parallel (default: 8)")
    add_auth_arguments(parser)


# -- IDs -------------------------------------------------------------------------------

def _project(pid: str) -> str:
    return node_id("gcp", "project", pid)


def _network(url: str) -> str:
    return node_id("gcp", "network", link_project(url), short(url))


def _region(network: str, region: str) -> str:
    _, _, project, name = network.split(":", 3)
    return node_id("gcp", "region", project, name, region)


def _regional(kind: str, url: str) -> str:
    return node_id("gcp", kind, link_project(url), link_region(url), short(url))


# -- one project -----------------------------------------------------------------------

class _Scope:
    """What one project's collection found, and what it wants to say."""

    def __init__(self, api: Api, pid: str, include: List[str]):
        self.api = api
        self.pid = pid
        self.include = set(include)
        self.topo = Topology()
        self.hints: Dict[str, Dict[str, Any]] = {}
        self.status = "ok"
        self.tunnel_gateways: Dict[str, str] = {}    # tunnel self link -> gateway node
        self.attachment_routers: Dict[str, str] = {}  # attachment self link -> router node

    def warn(self, message: str) -> None:
        self.topo.warn(f"gcp {self.pid}: {message}")

    def step(self, what: str, fn: Callable[[], Any]) -> Any:
        """Run one listing; a failure is a warning (or silence for an API
        the project simply doesn't use) and collection carries on."""
        try:
            return fn()
        except ApiError as e:
            if not (e.disabled and what in ("list NCC hubs", "list NCC spokes")):
                self.warn(f"cannot {what} ({e.code})")
            return None

    def region_node(self, network: str, region: str) -> str:
        rid = _region(network, region)
        if network not in self.topo.nodes:
            self.hints.setdefault(network, {"external": True})
            self.topo.add_node(network, GCP_NETWORK, short(network), _project(network.split(":")[2]),
                               external=True)
            self.topo.add_node(_project(network.split(":")[2]), GCP_PROJECT,
                               network.split(":")[2], project_id=network.split(":")[2])
        self.topo.add_node(rid, GCP_REGION, region, network, region=region)
        return rid


def _collect(api: Api, pid: str, include: List[str]) -> _Scope:
    scope = _Scope(api, pid, include)
    try:
        networks = api.pages(f"{COMPUTE}/projects/{pid}/global/networks", "items")
    except ApiError as e:
        scope.status = "disabled" if e.disabled else "denied"
        if scope.status == "denied":
            scope.warn(f"cannot list networks ({e.code}); project skipped")
        return scope
    scope.topo.add_node(_project(pid), GCP_PROJECT, pid, project_id=pid, collected=True)
    for network in networks:
        _add_network(scope, network)
    for what, fn in (
            ("list subnets", lambda: _subnets(scope)),
            ("list Cloud Routers", lambda: _routers(scope)),
            ("list VPN gateways", lambda: _vpn_gateways(scope)),
            ("list external VPN gateways", lambda: _external_gateways(scope)),
            ("list VPN tunnels", lambda: _tunnels(scope)),
            ("list interconnects", lambda: _interconnects(scope)),
            ("list interconnect attachments", lambda: _attachments(scope)),
            ("read Shared VPC", lambda: _shared_vpc(scope)),
            ("list NCC hubs", lambda: _ncc_hubs(scope)),
            ("list NCC spokes", lambda: _ncc_spokes(scope))):
        scope.step(what, fn)
    if "instances" in scope.include:
        scope.step("list instances", lambda: _instances(scope))
    return scope


def _add_network(scope: _Scope, network: Dict[str, Any]) -> None:
    nid = _network(network["selfLink"])
    peerings = []
    for peering in network.get("peerings", []):
        peer = _network(peering.get("network", ""))
        peerings.append({"name": peering.get("name"), "peer": peer.split(":", 2)[2],
                         "state": peering.get("state"),
                         "export_custom_routes": peering.get("exportCustomRoutes"),
                         "import_custom_routes": peering.get("importCustomRoutes")})
        scope.hints.setdefault(peer, {"project_id": link_project(peering.get("network"))})
        scope.topo.add_link(LINK_VPC_PEERING, nid, peer, label=peering.get("name", ""),
                            state=peering.get("state"), peerings=[peering.get("name")])
    scope.topo.add_node(
        nid, GCP_NETWORK, network["name"], _project(scope.pid), network=network["name"],
        project_id=scope.pid, routing_mode=(network.get("routingConfig") or {}).get("routingMode"),
        auto_subnets=network.get("autoCreateSubnetworks"), mtu=network.get("mtu"),
        legacy=True if network.get("IPv4Range") else None, cidr=network.get("IPv4Range"),
        peerings=peerings or None)


def _subnets(scope: _Scope) -> None:
    for subnet in scope.api.aggregated(f"{COMPUTE}/projects/{scope.pid}/aggregated/subnetworks",
                                       "subnetworks"):
        region = link_region(subnet.get("region"))
        parent = scope.region_node(_network(subnet["network"]), region)
        purpose = subnet.get("purpose")
        scope.topo.add_node(
            _regional("subnet", subnet["selfLink"]), GCP_SUBNET, subnet["name"], parent,
            subnet=subnet["name"], cidr=subnet.get("ipCidrRange"), region=region,
            secondary_ranges=[{"name": r.get("rangeName"), "cidr": r.get("ipCidrRange")}
                              for r in subnet.get("secondaryIpRanges", [])] or None,
            ipv6_cidrs=[c for c in (subnet.get("ipv6CidrRange"), subnet.get("externalIpv6Prefix"),
                                    subnet.get("internalIpv6Prefix")) if c] or None,
            private_google_access=subnet.get("privateIpGoogleAccess"),
            purpose=purpose if purpose not in (None, "PRIVATE", "PRIVATE_RFC_1918") else None,
            stack_type=subnet.get("stackType"),
            flow_logs=(subnet.get("logConfig") or {}).get("enable"))


def _routers(scope: _Scope) -> None:
    for router in scope.api.aggregated(f"{COMPUTE}/projects/{scope.pid}/aggregated/routers", "routers"):
        region = link_region(router.get("region"))
        parent = scope.region_node(_network(router["network"]), region)
        bgp = router.get("bgp") or {}
        scope.topo.add_node(
            _regional("router", router["selfLink"]), GCP_ROUTER, router["name"], parent,
            router=router["name"], region=region, asn=bgp.get("asn"),
            bgp_peers=[{"name": p.get("name"), "peer_ip": p.get("peerIpAddress"),
                        "peer_asn": p.get("peerAsn"), "ip": p.get("ipAddress"),
                        "interface": p.get("interfaceName")}
                       for p in router.get("bgpPeers", [])] or None,
            interfaces=[{"name": i.get("name"), "ip_range": i.get("ipRange"),
                         "vpn_tunnel": short(i.get("linkedVpnTunnel")) or None,
                         "attachment": short(i.get("linkedInterconnectAttachment")) or None}
                        for i in router.get("interfaces", [])] or None,
            nats=sorted(n.get("name") for n in router.get("nats", [])) or None)


def _vpn_gateways(scope: _Scope) -> None:
    base = f"{COMPUTE}/projects/{scope.pid}/aggregated"
    for gateway, kind in [(g, "ha") for g in scope.api.aggregated(f"{base}/vpnGateways", "vpnGateways")] + \
            [(g, "classic") for g in scope.api.aggregated(f"{base}/targetVpnGateways", "targetVpnGateways")]:
        region = link_region(gateway.get("region"))
        parent = scope.region_node(_network(gateway["network"]), region)
        scope.topo.add_node(
            _regional("vpn-gateway", gateway["selfLink"]), GCP_VPN_GATEWAY, gateway["name"], parent,
            gateway=gateway["name"], type=kind, region=region,
            interfaces=[{"id": i.get("id"), "ip": i.get("ipAddress")}
                        for i in gateway.get("vpnInterfaces", [])] or None)


def _external_gateways(scope: _Scope) -> None:
    for gateway in scope.api.pages(f"{COMPUTE}/projects/{scope.pid}/global/externalVpnGateways", "items"):
        interfaces = [{"id": i.get("id"), "ip": i.get("ipAddress")} for i in gateway.get("interfaces", [])]
        scope.topo.add_node(
            node_id("gcp", "external-vpn-gateway", scope.pid, gateway["name"]), GCP_PEER_GATEWAY,
            gateway["name"], _project(scope.pid), gateway=gateway["name"],
            redundancy=gateway.get("redundancyType"), interfaces=interfaces or None,
            ip=interfaces[0]["ip"] if interfaces else None)


def _tunnels(scope: _Scope) -> None:
    for tunnel in scope.api.aggregated(f"{COMPUTE}/projects/{scope.pid}/aggregated/vpnTunnels", "vpnTunnels"):
        own = tunnel.get("vpnGateway") or tunnel.get("targetVpnGateway")
        if not own:
            continue
        gateway = _regional("vpn-gateway", own)
        scope.tunnel_gateways[tunnel["selfLink"]] = gateway
        peer = _tunnel_peer(scope, tunnel)
        scope.topo.add_link(
            LINK_VPN, gateway, peer,
            tunnels=[{"name": tunnel["name"], "status": tunnel.get("status"),
                      "detail": tunnel.get("detailedStatus"), "peer_ip": tunnel.get("peerIp"),
                      "interface": tunnel.get("vpnGatewayInterface"),
                      "router": short(tunnel.get("router")) or None, "ike_version": tunnel.get("ikeVersion")}])


def _tunnel_peer(scope: _Scope, tunnel: Dict[str, Any]) -> str:
    """The far end: an external VPN gateway, another GCP HA VPN gateway, or
    (classic VPN) just an address, drawn as a peer gateway holding it so
    stitching treats both the same way."""
    if tunnel.get("peerExternalGateway"):
        url = tunnel["peerExternalGateway"]
        return node_id("gcp", "external-vpn-gateway", link_project(url), short(url))
    if tunnel.get("peerGcpGateway"):
        return _regional("vpn-gateway", tunnel["peerGcpGateway"])
    ip = tunnel.get("peerIp") or "unknown"
    pid = node_id("gcp", "external-vpn-gateway", scope.pid, f"peer-{ip}")
    scope.topo.add_node(pid, GCP_PEER_GATEWAY, f"VPN peer {ip}", _project(scope.pid),
                        ip=ip, interfaces=[{"id": 0, "ip": ip}])
    return pid


def _interconnects(scope: _Scope) -> None:
    for ic in scope.api.pages(f"{COMPUTE}/projects/{scope.pid}/global/interconnects", "items"):
        scope.topo.add_node(
            node_id("gcp", "interconnect", scope.pid, ic["name"]), GCP_INTERCONNECT, ic["name"],
            _project(scope.pid), interconnect=ic["name"], type=ic.get("interconnectType"),
            link_type=ic.get("linkType"), links=ic.get("provisionedLinkCount"),
            location=short(ic.get("location")), state=ic.get("operationalStatus"))


def _attachments(scope: _Scope) -> None:
    url = f"{COMPUTE}/projects/{scope.pid}/aggregated/interconnectAttachments"
    for att in scope.api.aggregated(url, "interconnectAttachments"):
        router = _regional("router", att.get("router", "")) if att.get("router") else None
        if router is None:
            continue
        scope.attachment_routers[att["selfLink"]] = router
        if att.get("interconnect"):
            target = node_id("gcp", "interconnect", link_project(att["interconnect"]), short(att["interconnect"]))
            scope.hints.setdefault(target, {"type": "DEDICATED"})
        else:
            partner = (att.get("partnerMetadata") or {}).get("partnerName") or "Partner Interconnect"
            target = node_id("gcp", "interconnect", scope.pid, f"partner-{att['name']}")
            scope.topo.add_node(target, GCP_INTERCONNECT, partner, _project(scope.pid), type="PARTNER",
                                partner_name=partner,
                                interconnect_name=(att.get("partnerMetadata") or {}).get("interconnectName"))
        scope.topo.add_link(
            LINK_INTERCONNECT_ATTACHMENT, router, target, key=att["name"], label=att["name"],
            attachment=att["name"], type=att.get("type"), vlan=att.get("vlanTag8021q"),
            cloud_router_address=att.get("cloudRouterIpAddress"),
            customer_address=att.get("customerRouterIpAddress"), state=att.get("state"),
            bandwidth=att.get("bandwidth"), edge_availability_domain=att.get("edgeAvailabilityDomain"),
            admin_enabled=att.get("adminEnabled"))


def _shared_vpc(scope: _Scope) -> None:
    project = scope.api.get(f"{COMPUTE}/projects/{scope.pid}")
    if project.get("xpnProjectStatus") != "HOST":
        return
    host = _project(scope.pid)
    scope.topo.nodes[host].attrs["shared_vpc"] = "host"
    resources = scope.api.pages(f"{COMPUTE}/projects/{scope.pid}/getXpnResources", "resources")
    for resource in resources:
        if resource.get("type") != "PROJECT":
            continue
        service = _project(resource["id"])
        scope.topo.add_node(service, GCP_PROJECT, resource["id"], project_id=resource["id"],
                            shared_vpc="service", shared_vpc_host=scope.pid)
        scope.topo.add_link(LINK_SHARED_VPC, service, host, directed=True, host_project=scope.pid)


def _ncc_hubs(scope: _Scope) -> None:
    for hub in scope.api.pages(f"{NCC}/projects/{scope.pid}/locations/global/hubs", "hubs"):
        scope.topo.add_node(node_id("gcp", "ncc-hub", scope.pid, short(hub["name"])), GCP_NCC_HUB,
                            short(hub["name"]), _project(scope.pid), hub=short(hub["name"]),
                            state=hub.get("state"))


def _ncc_spokes(scope: _Scope) -> None:
    for spoke in scope.api.pages(f"{NCC}/projects/{scope.pid}/locations/-/spokes", "spokes"):
        hub_name = spoke.get("hub", "")
        parts = hub_name.split("/")
        hub_project = parts[1] if len(parts) > 1 else scope.pid
        hub = node_id("gcp", "ncc-hub", hub_project, short(hub_name))
        scope.hints.setdefault(hub, {})
        targets = []
        vpc = (spoke.get("linkedVpcNetwork") or {}).get("uri")
        if vpc:
            targets.append(_network(vpc))
        for uri in (spoke.get("linkedVpnTunnels") or {}).get("uris", []):
            gateway = scope.tunnel_gateways.get(uri)
            if gateway:
                targets.append(gateway)
        for uri in (spoke.get("linkedInterconnectAttachments") or {}).get("uris", []):
            router = scope.attachment_routers.get(uri)
            if router:
                targets.append(router)
        for target in dict.fromkeys(targets):
            scope.topo.add_link(LINK_NCC_SPOKE, hub, target, directed=True, key=short(spoke["name"]),
                                spoke=short(spoke["name"]), state=spoke.get("state"))


def _instances(scope: _Scope) -> None:
    for vm in scope.api.aggregated(f"{COMPUTE}/projects/{scope.pid}/aggregated/instances", "instances"):
        nics = vm.get("networkInterfaces", [])
        subnet = _regional("subnet", nics[0]["subnetwork"]) if nics and nics[0].get("subnetwork") else None
        parent = subnet if subnet in scope.topo.nodes else _project(scope.pid)
        scope.topo.add_node(
            node_id("gcp", "instance", scope.pid, short(vm.get("zone")), vm["name"]), GCP_INSTANCE,
            vm["name"], parent, instance=vm["name"], zone=short(vm.get("zone")),
            machine_type=short(vm.get("machineType")), status=vm.get("status"),
            private_ips=[n.get("networkIP") for n in nics if n.get("networkIP")] or None,
            public_ips=[a.get("natIP") for n in nics for a in n.get("accessConfigs", [])
                        if a.get("natIP")] or None)


# -- assembly --------------------------------------------------------------------------

def _projects(api: Api, args_projects: List[str], folders: List[str], organization: Optional[str],
              warnings: List[str]) -> List[str]:
    found = list(args_projects)
    roots = [f"folders/{short(f)}" for f in folders]
    if organization:
        roots.append(f"organizations/{short(organization)}")
    for root in roots:
        for _, _, _, projects in walk(api, root, lambda m: warnings.append(f"gcp: {m}")):
            found.extend(p["projectId"] for p in projects)
    if not found:
        raise ValueError("nothing to collect: pass --project, --folder or --organization")
    return sorted(set(found))


def _placeholders(topo: Topology, hints: Dict[str, Dict[str, Any]]) -> None:
    """Link ends nobody collected (a peered network in another project, an
    interconnect owned elsewhere) get a node marked `external`."""
    for link in sorted(topo.links.values(), key=lambda l: l.id):
        for end in (link.source, link.target):
            if end in topo.nodes:
                continue
            parts = end.split(":")
            kind = _PLACEHOLDER_KINDS.get(parts[1], NET_EXTERNAL)
            topo.add_node(end, kind, parts[-1], external=True, **hints.get(end, {}))


def collect(api: Api, projects: List[str], include: List[str] = (), workers: int = 8,
            warnings: Optional[List[str]] = None) -> Topology:
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        scopes = list(pool.map(lambda p: _collect(api, p, list(include)), projects))
    first = Topology()
    for warning in warnings or []:
        first.warn(warning)
    topo = union([first] + [s.topo for s in sorted(scopes, key=lambda s: s.pid)])
    hints: Dict[str, Dict[str, Any]] = {}
    for scope in scopes:
        for nid, attrs in scope.hints.items():
            hints.setdefault(nid, {}).update({k: v for k, v in attrs.items() if v is not None})
    _placeholders(topo, hints)
    disabled = sorted(s.pid for s in scopes if s.status == "disabled")
    if disabled:
        topo.warn(f"gcp: Compute Engine API not enabled in {len(disabled)} project(s); skipped "
                  "(listed in meta.compute_disabled)")
    meta: Dict[str, Any] = {
        "collector": "gcp",
        "projects": sorted(s.pid for s in scopes if s.status == "ok"),
        "compute_disabled": disabled or None,
        "collected_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if topo.meta.get("warnings"):
        meta["warnings"] = topo.meta["warnings"]
    topo.meta = {k: v for k, v in meta.items() if v is not None}
    return topo


def run(args) -> Topology:
    api = api_from_args(args)
    warnings: List[str] = []
    projects = _projects(api, args.project, args.folder, args.organization, warnings)
    return collect(api, projects, sorted(set(args.include or [])), args.workers, warnings)
