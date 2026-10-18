"""Collect a Google Cloud organization: folders, projects, Assured Workloads
and org policies (read-only).

Run it with credentials that can browse the organization: Organization
Viewer and Folder Viewer (`roles/resourcemanager.organizationViewer`,
`roles/resourcemanager.folderViewer`) are enough for the hierarchy; Assured
Workloads Reader and Org Policy Viewer add those parts. Anything denied is
a warning, and the rest is still drawn.

Projects get the same IDs the `gcp` network collector gives them
(`gcp:project:<project-id>`), so merging the two snapshots draws each
project, with its networks, inside its folder.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..model import GCP_FOLDER, GCP_ORG, GCP_PROJECT, Topology, node_id
from .gcp_api import (ASSURED_WORKLOADS, ORG_POLICY, Api, ApiError, add_auth_arguments,
                      api_from_args, find_organization, short, walk)

HELP = ("Collect a Google Cloud organization: folders, projects, Assured Workloads and org "
        "policies (read-only).")
PRODUCES_TOPOLOGY = True

EXTRAS = ("labels",)
# Assured Workloads lives in a location; these are tried when the API
# can't list its own locations. --aw-location adds more.
AW_LOCATIONS = ("us", "global", "eu", "us-central1", "us-east4", "us-west1", "europe-west2",
                "europe-west3", "asia-northeast1", "australia-southeast1", "northamerica-northeast1")


def _include_list(value: str) -> List[str]:
    items = [v.strip() for v in value.split(",") if v.strip()]
    unknown = [v for v in items if v not in EXTRAS]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown --include value {', '.join(unknown)} (choose from {', '.join(EXTRAS)})")
    return items


def add_arguments(parser) -> None:
    parser.add_argument("--organization", metavar="ID",
                        help="organization ID (default: the only organization the credentials "
                             "can see)")
    parser.add_argument("--policies", choices=("attrs", "none"), default="attrs",
                        help="read the org policy constraints set on the organization, each folder "
                             "and each project (default), or skip them")
    parser.add_argument("--aw-location", action="append", default=[], metavar="LOCATION",
                        help="also look for Assured Workloads in this location (repeatable)")
    parser.add_argument("--include", action="extend", type=_include_list, default=[],
                        metavar="LIST", help="comma-separated extras: labels (project labels)")
    parser.add_argument("--workers", type=int, default=8, metavar="N",
                        help="org policy lookups in parallel (default: 8)")
    add_auth_arguments(parser)


class _Org:
    def __init__(self, api: Api, include: List[str]):
        self.api = api
        self.include = set(include)
        self.topo = Topology()
        self.nodes: Dict[str, str] = {}     # resource name ("folders/1") -> node ID
        self.numbers: Dict[str, str] = {}   # project number -> node ID

    def warn(self, message: str) -> None:
        self.topo.warn(f"gcp-org: {message}")


def _hierarchy(org: _Org, organization: Dict[str, Any]) -> str:
    org_name = organization["name"]                 # "organizations/123"
    oid = node_id("gcp", "organization", short(org_name))
    domain = organization.get("displayName") or short(org_name)
    org.topo.add_node(oid, GCP_ORG, f"{domain} ({short(org_name)})", org_id=short(org_name),
                      domain=organization.get("displayName"),
                      directory_customer_id=organization.get("directoryCustomerId"))
    org.nodes[org_name] = oid
    for parent, path, folders, projects in walk(org.api, org_name, org.warn):
        parent_node = org.nodes[parent]
        for folder in folders:
            fid = node_id("gcp", "folder", short(folder["name"]))
            name = folder.get("displayName") or short(folder["name"])
            org.topo.add_node(fid, GCP_FOLDER, name, parent_node, folder_id=short(folder["name"]),
                              path=f"{path}/{name}".lstrip("/"))
            org.nodes[folder["name"]] = fid
        for project in projects:
            _project(org, project, parent_node, path)
    return oid


def _project(org: _Org, project: Dict[str, Any], parent_node: str, path: str) -> None:
    pid = project["projectId"]
    nid = node_id("gcp", "project", pid)
    number = short(project.get("name"))
    display = project.get("displayName") or pid
    label = pid if display == pid else f"{display} ({pid})"
    org.topo.add_node(nid, GCP_PROJECT, label, parent_node, project_id=pid,
                      project_number=number or None, name=project.get("displayName") or None,
                      path=path or None,
                      labels=project.get("labels") or None if "labels" in org.include else None)
    org.nodes[f"projects/{number}"] = nid
    org.nodes[f"projects/{pid}"] = nid
    org.numbers[number] = nid


# -- Assured Workloads -----------------------------------------------------------------

def _aw_locations(org: _Org, org_id: str, extra: List[str]) -> List[str]:
    try:
        listed = org.api.pages(f"{ASSURED_WORKLOADS}/organizations/{org_id}/locations", "locations")
        found = [l.get("locationId") for l in listed if l.get("locationId")]
    except ApiError:
        found = []
    return sorted(set(found or AW_LOCATIONS) | set(extra))


def _assured_workloads(org: _Org, org_id: str, extra_locations: List[str]) -> int:
    """Attach each workload to the folder it governs (and mark its key
    project). Returns how many workloads were found."""
    count = 0
    for location in _aw_locations(org, org_id, extra_locations):
        url = f"{ASSURED_WORKLOADS}/organizations/{org_id}/locations/{location}/workloads"
        try:
            workloads = org.api.pages(url, "workloads")
        except ApiError as e:
            if e.missing:
                continue  # not a location this organization uses
            org.warn(f"cannot list Assured Workloads in {location} ({e.code})")
            if e.denied or e.disabled:
                return count
            continue
        for workload in workloads:
            count += 1
            _attach_workload(org, workload, location)
    return count


def _attach_workload(org: _Org, workload: Dict[str, Any], location: str) -> None:
    status = workload.get("complianceStatus") or {}
    summary = {
        "name": short(workload.get("name")),
        "display_name": workload.get("displayName"),
        "compliance_regime": workload.get("complianceRegime"),
        "location": location,
        "partner": workload.get("partner"),
        "sovereign_controls": workload.get("enableSovereignControls"),
        "active_violations": status.get("activeViolationCount"),
        "acknowledged_violations": status.get("acknowledgedViolationCount"),
    }
    summary = {k: v for k, v in summary.items() if v not in (None, "")}
    for resource in workload.get("resources", []):
        rid = str(resource.get("resourceId", ""))
        rtype = resource.get("resourceType", "")
        if rtype == "CONSUMER_FOLDER":
            node = org.topo.nodes.get(org.nodes.get(f"folders/{rid}", ""))
            if node is not None:
                node.attrs["assured_workload"] = summary
        elif rtype in ("ENCRYPTION_KEYS_PROJECT", "CONSUMER_PROJECT"):
            node = org.topo.nodes.get(org.numbers.get(rid, ""))
            if node is not None:
                node.attrs["assured_workload_role"] = (
                    "keys" if rtype == "ENCRYPTION_KEYS_PROJECT" else "consumer")
                node.attrs["assured_workload"] = summary


# -- org policies ----------------------------------------------------------------------

def _org_policies(org: _Org, workers: int) -> None:
    """The constraints set (not inherited) on each node. One call per node,
    in parallel; the first denial stops further lookups with one warning."""
    names = sorted(org.nodes)
    names = [n for n in names if not (n.startswith("projects/") and n.split("/")[1].isdigit())]
    denied: List[str] = []

    def lookup(name: str) -> Optional[List[str]]:
        if denied:
            return None
        try:
            policies = org.api.pages(f"{ORG_POLICY}/{name}/policies", "policies")
        except ApiError as e:
            if e.missing:
                return []
            denied.append(e.code)
            return None
        return sorted(short(p.get("name")) for p in policies)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for name, constraints in zip(names, pool.map(lookup, names)):
            if constraints:
                org.topo.nodes[org.nodes[name]].attrs["org_policies"] = constraints
    if denied:
        org.warn(f"cannot read org policies ({denied[0]}); they are missing")


# -- entry points ----------------------------------------------------------------------

def collect(api: Api, organization: Optional[str] = None, policies: str = "attrs",
            include: List[str] = (), aw_locations: List[str] = (), workers: int = 8) -> Topology:
    org = _Org(api, list(include))
    try:
        found = find_organization(api, organization)
    except ApiError as e:
        raise RuntimeError(f"cannot read the organization ({e.code}): the credentials need "
                           "roles/resourcemanager.organizationViewer on it") from None
    oid = _hierarchy(org, found)
    workloads = _assured_workloads(org, short(found["name"]), list(aw_locations))
    if policies != "none":
        _org_policies(org, workers)
    topo = org.topo
    meta: Dict[str, Any] = {
        "collector": "gcp-org",
        "organization": topo.nodes[oid].attrs["org_id"],
        "folders": sorted(n.attrs["folder_id"] for n in topo.by_kind(GCP_FOLDER)),
        "projects": sorted(n.attrs["project_id"] for n in topo.by_kind(GCP_PROJECT)),
        "assured_workloads": workloads,
        "collected_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if topo.meta.get("warnings"):
        meta["warnings"] = topo.meta["warnings"]
    topo.meta = meta
    return topo


def run(args) -> Topology:
    return collect(api_from_args(args), args.organization, args.policies,
                   sorted(set(args.include or [])), args.aw_location, args.workers)
