"""A fake Google Cloud estate behind a fake HTTP transport, for the GCP
collector tests. Shaped like a real Assured Workloads setup: everything sits
under one Assured Workloads folder. Names and addresses are made up
(example.net, RFC 1918, documentation and link-local ranges)."""

import os
import sys
from typing import Any, Dict, List, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from topomap.collectors.gcp_api import (ASSURED_WORKLOADS, COMPUTE, CRM, NCC,  # noqa: E402
                                        ORG_POLICY, Api)

ORG = "123456789012"
AW_FOLDER, PROD, NONPROD, SHARED = "111111111111", "222222222222", "333333333333", "444444444444"
HOST, KEYS, APP_PROD, APP_DEV = "net-host-prod", "aw-keys-1a2b", "app-prod", "app-dev"
NUMBERS = {HOST: "100000000001", KEYS: "100000000002", APP_PROD: "100000000003",
           APP_DEV: "100000000004"}
ONPREM_VPN_IP = "198.51.100.7"
IC_CUSTOMER_IP = "169.254.20.2"


def _self(project: str, path: str) -> str:
    return f"{COMPUTE}/projects/{project}/{path}"


def _folder(fid: str, name: str, parent: str) -> Dict[str, Any]:
    return {"name": f"folders/{fid}", "displayName": name, "parent": parent, "state": "ACTIVE"}


def _project(pid: str, parent: str, **extra: Any) -> Dict[str, Any]:
    return {"name": f"projects/{NUMBERS[pid]}", "projectId": pid, "displayName": pid,
            "parent": parent, "state": "ACTIVE", **extra}


def _disabled(api: str) -> Tuple[int, Any]:
    return 403, {"error": {"code": 403, "status": "PERMISSION_DENIED",
                           "message": f"{api} has not been used in project before or it is disabled.",
                           "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo",
                                        "reason": "SERVICE_DISABLED"}]}}


def _denied() -> Tuple[int, Any]:
    return 403, {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "denied"}}


def routes() -> Dict[str, Any]:
    net = _self(HOST, "global/networks/shared-vpc")
    r = {}
    # -- Resource Manager
    r[f"{CRM}/organizations:search"] = {"organizations": [
        {"name": f"organizations/{ORG}", "displayName": "example.net", "directoryCustomerId": "C0xample"}]}
    r[f"{CRM}/organizations/{ORG}"] = r[f"{CRM}/organizations:search"]["organizations"][0]
    r[(f"{CRM}/folders", f"organizations/{ORG}")] = {"folders": [_folder(AW_FOLDER, "Assured Workloads", f"organizations/{ORG}")]}
    r[(f"{CRM}/folders", f"folders/{AW_FOLDER}")] = {"folders": [
        _folder(PROD, "Prod", f"folders/{AW_FOLDER}"), _folder(NONPROD, "NonProd", f"folders/{AW_FOLDER}"),
        _folder(SHARED, "Shared", f"folders/{AW_FOLDER}")]}
    r[(f"{CRM}/projects", f"folders/{AW_FOLDER}")] = {"projects": [_project(KEYS, f"folders/{AW_FOLDER}")]}
    r[(f"{CRM}/projects", f"folders/{SHARED}")] = {"projects": [
        _project(HOST, f"folders/{SHARED}", labels={"team": "network"})]}
    r[(f"{CRM}/projects", f"folders/{PROD}")] = {"projects": [_project(APP_PROD, f"folders/{PROD}")]}
    r[(f"{CRM}/projects", f"folders/{NONPROD}")] = {"projects": [_project(APP_DEV, f"folders/{NONPROD}")]}
    # -- Assured Workloads (locations aren't listable here: the collector falls back)
    r[f"{ASSURED_WORKLOADS}/organizations/{ORG}/locations/us/workloads"] = {"workloads": [{
        "name": f"organizations/{ORG}/locations/us/workloads/fedramp-mod-1",
        "displayName": "FedRAMP Moderate", "complianceRegime": "FEDRAMP_MODERATE",
        "complianceStatus": {"activeViolationCount": 2, "acknowledgedViolationCount": 1},
        "resources": [{"resourceId": int(AW_FOLDER), "resourceType": "CONSUMER_FOLDER"},
                      {"resourceId": int(NUMBERS[KEYS]), "resourceType": "ENCRYPTION_KEYS_PROJECT"}]}]}
    # -- org policies (set, not inherited)
    r[f"{ORG_POLICY}/organizations/{ORG}/policies"] = {"policies": [
        {"name": f"organizations/{ORG}/policies/gcp.resourceLocations"},
        {"name": f"organizations/{ORG}/policies/compute.vmExternalIpAccess"}]}
    r[f"{ORG_POLICY}/folders/{AW_FOLDER}/policies"] = {"policies": [
        {"name": f"folders/{AW_FOLDER}/policies/gcp.restrictNonCmekServices"}]}
    # -- Compute: the Shared VPC host
    r[_self(HOST, "global/networks")] = {"items": [{
        "name": "shared-vpc", "selfLink": net, "autoCreateSubnetworks": False, "mtu": 1460,
        "routingConfig": {"routingMode": "GLOBAL"},
        "peerings": [{"name": "to-partner", "network": _self("partner-proj", "global/networks/partner-net"),
                      "state": "ACTIVE", "exportCustomRoutes": True}]}]}
    r[_self(HOST, "aggregated/subnetworks")] = [
        {"items": {"regions/us-east4": {"subnetworks": [{
            "name": "prod-use4", "selfLink": _self(HOST, "regions/us-east4/subnetworks/prod-use4"),
            "network": net, "region": _self(HOST, "regions/us-east4"), "ipCidrRange": "10.10.0.0/20",
            "privateIpGoogleAccess": True,
            "secondaryIpRanges": [{"rangeName": "pods", "ipCidrRange": "10.64.0.0/14"}]}]}},
         "nextPageToken": "page2"},
        {"items": {"regions/us-central1": {"subnetworks": [{
            "name": "prod-usc1", "selfLink": _self(HOST, "regions/us-central1/subnetworks/prod-usc1"),
            "network": net, "region": _self(HOST, "regions/us-central1"), "ipCidrRange": "10.10.16.0/20"}]}}}]
    r[_self(HOST, "aggregated/routers")] = {"items": {"regions/us-east4": {"routers": [{
        "name": "cr-use4", "selfLink": _self(HOST, "regions/us-east4/routers/cr-use4"), "network": net,
        "region": _self(HOST, "regions/us-east4"), "bgp": {"asn": 64512},
        "bgpPeers": [{"name": "vpn-peer-0", "peerIpAddress": "169.254.10.2", "peerAsn": 65010,
                      "ipAddress": "169.254.10.1", "interfaceName": "if-tun-0"},
                     {"name": "ic-peer", "peerIpAddress": IC_CUSTOMER_IP, "peerAsn": 65010,
                      "ipAddress": "169.254.20.1", "interfaceName": "if-att-1"}],
        "interfaces": [{"name": "if-tun-0", "ipRange": "169.254.10.1/30",
                        "linkedVpnTunnel": _self(HOST, "regions/us-east4/vpnTunnels/tun-0")}],
        "nats": [{"name": "nat-use4"}]}]}}}
    r[_self(HOST, "aggregated/vpnGateways")] = {"items": {"regions/us-east4": {"vpnGateways": [{
        "name": "ha-vpn-use4", "selfLink": _self(HOST, "regions/us-east4/vpnGateways/ha-vpn-use4"),
        "network": net, "region": _self(HOST, "regions/us-east4"),
        "vpnInterfaces": [{"id": 0, "ipAddress": "203.0.113.10"}, {"id": 1, "ipAddress": "203.0.113.11"}]}]}}}
    r[_self(HOST, "aggregated/targetVpnGateways")] = {"items": {}}
    r[_self(HOST, "global/externalVpnGateways")] = {"items": [{
        "name": "onprem-edge", "redundancyType": "SINGLE_IP_INTERNALLY_REDUNDANT",
        "interfaces": [{"id": 0, "ipAddress": ONPREM_VPN_IP}]}]}
    tunnel = lambda name, iface: {  # noqa: E731
        "name": name, "selfLink": _self(HOST, f"regions/us-east4/vpnTunnels/{name}"),
        "region": _self(HOST, "regions/us-east4"),
        "vpnGateway": _self(HOST, "regions/us-east4/vpnGateways/ha-vpn-use4"), "vpnGatewayInterface": iface,
        "peerExternalGateway": _self(HOST, "global/externalVpnGateways/onprem-edge"),
        "peerIp": ONPREM_VPN_IP, "status": "ESTABLISHED", "detailedStatus": "Tunnel is up and running.",
        "router": _self(HOST, "regions/us-east4/routers/cr-use4"), "ikeVersion": 2,
        "sharedSecretHash": "never-read"}
    r[_self(HOST, "aggregated/vpnTunnels")] = {"items": {"regions/us-east4": {
        "vpnTunnels": [tunnel("tun-0", 0), tunnel("tun-1", 1)]}}}
    r[_self(HOST, "global/interconnects")] = {"items": [{
        "name": "ic-dedicated-1", "interconnectType": "DEDICATED", "linkType": "LINK_TYPE_ETHERNET_10G_LR",
        "provisionedLinkCount": 2, "operationalStatus": "OS_ACTIVE",
        "location": f"{COMPUTE}/projects/{HOST}/global/interconnectLocations/iad-zone1-1"}]}
    r[_self(HOST, "aggregated/interconnectAttachments")] = {"items": {"regions/us-east4": {
        "interconnectAttachments": [
            {"name": "att-1", "selfLink": _self(HOST, "regions/us-east4/interconnectAttachments/att-1"),
             "type": "DEDICATED", "router": _self(HOST, "regions/us-east4/routers/cr-use4"),
             "interconnect": _self(HOST, "global/interconnects/ic-dedicated-1"), "vlanTag8021q": 100,
             "cloudRouterIpAddress": "169.254.20.1/29", "customerRouterIpAddress": f"{IC_CUSTOMER_IP}/29",
             "state": "ACTIVE", "edgeAvailabilityDomain": "AVAILABILITY_DOMAIN_1"},
            {"name": "att-partner", "selfLink": _self(HOST, "regions/us-east4/interconnectAttachments/att-partner"),
             "type": "PARTNER", "router": _self(HOST, "regions/us-east4/routers/cr-use4"), "vlanTag8021q": 200,
             "partnerMetadata": {"partnerName": "Example Carrier", "interconnectName": "ex-carrier-iad"},
             "state": "ACTIVE"}]}}}
    r[_self(HOST, "")[:-1]] = {"name": HOST, "xpnProjectStatus": "HOST",
                               "commonInstanceMetadata": {"items": [{"key": "ssh-keys", "value": "never-read"}]}}
    r[_self(HOST, "getXpnResources")] = {"resources": [{"id": APP_PROD, "type": "PROJECT"}]}
    r[f"{NCC}/projects/{HOST}/locations/global/hubs"] = {"hubs": [
        {"name": f"projects/{HOST}/locations/global/hubs/global-hub", "state": "ACTIVE"}]}
    r[f"{NCC}/projects/{HOST}/locations/-/spokes"] = {"spokes": [{
        "name": f"projects/{HOST}/locations/us-east4/spokes/vpn-spoke",
        "hub": f"projects/{HOST}/locations/global/hubs/global-hub", "state": "ACTIVE",
        "linkedVpnTunnels": {"uris": [_self(HOST, "regions/us-east4/vpnTunnels/tun-0")]}}]}
    r[_self(HOST, "aggregated/instances")] = {"items": {"zones/us-east4-a": {"instances": [{
        "name": "jump-1", "zone": _self(HOST, "zones/us-east4-a"),
        "machineType": _self(HOST, "zones/us-east4-a/machineTypes/e2-small"), "status": "RUNNING",
        "networkInterfaces": [{"subnetwork": _self(HOST, "regions/us-east4/subnetworks/prod-use4"),
                               "networkIP": "10.10.0.5", "accessConfigs": [{"natIP": "203.0.113.50"}]}]}]}}}
    # -- Compute: a service project (no networks of its own) and one without Compute
    r[_self(APP_PROD, "global/networks")] = {"items": []}
    r[_self(APP_PROD, "")[:-1]] = {"name": APP_PROD, "xpnProjectStatus": "UNSPECIFIED_XPN_PROJECT_STATUS"}
    r[f"{NCC}/projects/{APP_PROD}/locations/global/hubs"] = _disabled("Network Connectivity API")
    r[f"{NCC}/projects/{APP_PROD}/locations/-/spokes"] = _disabled("Network Connectivity API")
    r[_self(APP_DEV, "global/networks")] = _disabled("Compute Engine API")
    r[_self(KEYS, "global/networks")] = _disabled("Compute Engine API")
    return r


class FakeGoogle:
    """A transport serving `routes()`. A key is a URL, or (URL, parent) for
    Resource Manager listings; a list value is a sequence of pages."""

    def __init__(self, overrides: Dict[Any, Any] = None):
        self.routes = routes()
        self.routes.update(overrides or {})
        self.calls: List[Tuple[str, Dict[str, Any]]] = []

    def __call__(self, url: str, params: Dict[str, Any]) -> Tuple[int, Any]:
        self.calls.append((url, dict(params)))
        value = self.routes.get((url, params.get("parent")), self.routes.get(url))
        if value is None:
            if url.startswith((f"{CRM}/folders", f"{CRM}/projects", COMPUTE)):
                return 200, {}  # an empty listing, as Google returns it
            return 404, {"error": {"code": 404, "status": "NOT_FOUND", "message": "not found"}}
        if isinstance(value, list):
            page = 1 if params.get("pageToken") == "page2" else 0
            return 200, value[page]
        if isinstance(value, tuple):
            return value
        return 200, value


def api(overrides: Dict[Any, Any] = None) -> Tuple[Api, FakeGoogle]:
    fake = FakeGoogle(overrides)
    return Api(fake, backoff=0), fake
