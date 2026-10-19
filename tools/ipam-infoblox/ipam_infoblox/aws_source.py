"""Read an IPAM straight from AWS instead of from an export (read-only).

`--from-aws` asks the IPAM itself, in its home region, for what an export
would hold, and builds the same table the spreadsheet reader does, so the
cleaning, planning, Grid check and report are unchanged:

* every pool in the scope, with each provisioned CIDR (type `pool`);
* each pool's custom allocations, which reserve a range for something IPAM
  doesn't see (type `allocation`);
* every resource CIDR IPAM monitors in the scope: VPCs, subnets, public IPv4
  and IPv6 pools, Elastic IPs and ENIs, across the accounts and operating
  regions the IPAM covers, with their resource tags as `tag:KEY` columns.

No column names are guessed, which is the point. `--save-export` writes the
table as CSV, a snapshot to review or keep with the change, and the
report's row numbers point into it.

Only Describe/Get/List calls can be made: the client is wrapped so anything
else is refused before a request is built. `AmazonEC2ReadOnlyAccess`
covers every call.
"""

from __future__ import annotations

import csv
import io
from typing import Any, Dict, Iterable, List, Optional

from .reader import Table

HEADERS = ["Resource ID", "Resource name", "Resource type", "Resource CIDR", "Region",
           "Resource owner", "VPC ID", "IPAM pool ID", "Description", "Management state",
           "Compliance status", "Overlap status", "IP usage"]
SCOPES = ("private", "public")
# Pool CIDRs that are in the pool now (deprovisioning hasn't finished yet).
_LIVE_POOL_CIDRS = {"provisioned", "pending-deprovision", "failed-deprovision"}
_DEAD_POOLS = {"create-failed", "delete-in-progress", "delete-complete", "delete-failed"}
READ_ONLY_PREFIXES = ("describe_", "get_", "list_")
PERMISSIONS = ("ec2:DescribeIpams, ec2:DescribeIpamScopes, ec2:DescribeIpamPools, "
               "ec2:GetIpamPoolCidrs, ec2:GetIpamPoolAllocations, ec2:GetIpamResourceCidrs "
               "(all in AmazonEC2ReadOnlyAccess)")


class ReadOnlyClient:
    """A boto3 client that can only describe, get and list."""

    def __init__(self, client: Any):
        self._client = client

    def __getattr__(self, name: str) -> Any:
        if name in ("meta", "exceptions", "can_paginate") or name.startswith(READ_ONLY_PREFIXES):
            return getattr(self._client, name)
        raise PermissionError(f"ipam-infoblox is read-only: refusing to call {name}")

    def get_paginator(self, operation: str) -> Any:
        if not operation.startswith(READ_ONLY_PREFIXES):
            raise PermissionError(f"ipam-infoblox is read-only: refusing to paginate {operation}")
        return self._client.get_paginator(operation)


def ec2_client(profile: Optional[str] = None, region: Optional[str] = None) -> ReadOnlyClient:
    try:
        import boto3
    except ImportError:
        raise RuntimeError("--from-aws needs boto3: pip install 'ipam-infoblox[aws]'") from None
    from botocore.config import Config
    from botocore.exceptions import BotoCoreError, ProfileNotFound
    try:
        session = boto3.Session(profile_name=profile, region_name=region)
    except ProfileNotFound as e:
        raise ValueError(str(e)) from None
    if not session.region_name:
        raise ValueError("--from-aws needs the IPAM's home region: --aws-region, or a region in "
                         "the profile")
    try:
        client = session.client("ec2", config=Config(retries={"mode": "standard",
                                                               "max_attempts": 10},
                                                     user_agent_extra="ipam-infoblox"))
    except BotoCoreError as e:
        raise ValueError(f"cannot set up AWS: {e}") from None
    return ReadOnlyClient(client)


def _all(client: ReadOnlyClient, operation: str, key: str, **kwargs: Any) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for page in client.get_paginator(operation).paginate(**kwargs):
        items.extend(page.get(key, []))
    return items


def _tags(tags: Optional[Iterable[Dict[str, str]]]) -> Dict[str, str]:
    return {t.get("Key", ""): t.get("Value", "") for t in tags or [] if t.get("Key")}


def _row(**values: Any) -> Dict[str, str]:
    row = {h: "" for h in HEADERS}
    for header, value in values.items():
        row[header] = "" if value is None else str(value)
    return row


def _call(what: str, fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Turn AWS errors into one clear message naming the permission."""
    from botocore.exceptions import BotoCoreError, ClientError
    try:
        return fn(*args, **kwargs)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("UnauthorizedOperation", "AccessDenied", "AccessDeniedException"):
            raise RuntimeError(f"AWS refused to {what} ({code}); --from-aws needs {PERMISSIONS}") from None
        raise RuntimeError(f"cannot {what}: {code or e}") from None
    except BotoCoreError as e:
        raise RuntimeError(f"cannot {what}: {e}") from None


def choose_ipam(client: ReadOnlyClient, ipam_id: Optional[str]) -> Dict[str, Any]:
    region = client.meta.region_name
    if ipam_id:
        found = _call(f"describe IPAM {ipam_id}", _all, client, "describe_ipams", "Ipams",
                      IpamIds=[ipam_id])
    else:
        found = _call("list IPAMs", _all, client, "describe_ipams", "Ipams")
    if not found:
        raise ValueError(f"no IPAM in {region}; pass the IPAM's home region with --aws-region")
    if len(found) > 1:
        raise ValueError(f"{len(found)} IPAMs in {region} "
                         f"({', '.join(sorted(i['IpamId'] for i in found))}); pick one with --ipam-id")
    return found[0]


def choose_scope(client: ReadOnlyClient, ipam: Dict[str, Any], scope: str) -> Dict[str, Any]:
    """The default private or public scope, or a scope by ID."""
    scopes = _call("list IPAM scopes", _all, client, "describe_ipam_scopes", "IpamScopes")
    scopes = [s for s in scopes if s.get("IpamArn") == ipam.get("IpamArn")]
    if scope in SCOPES:
        wanted = ipam.get("PrivateDefaultScopeId" if scope == "private" else "PublicDefaultScopeId")
    else:
        wanted = scope
    for found in scopes:
        if found.get("IpamScopeId") == wanted:
            return found
    raise ValueError(f"IPAM {ipam.get('IpamId')} has no scope {scope!r} (scopes: "
                     f"{', '.join(sorted(s.get('IpamScopeId', '') for s in scopes))})")


def fetch(client: ReadOnlyClient, ipam_id: Optional[str] = None, scope: str = "private") -> Table:
    """The IPAM's pools, allocations and resource CIDRs in one scope, as a table."""
    ipam = choose_ipam(client, ipam_id)
    chosen = choose_scope(client, ipam, scope)
    scope_id, scope_arn = chosen["IpamScopeId"], chosen.get("IpamScopeArn")
    rows: List[Dict[str, str]] = []
    tag_keys: set = set()

    pools = _call("list IPAM pools", _all, client, "describe_ipam_pools", "IpamPools")
    pools = sorted((p for p in pools if p.get("IpamScopeArn") == scope_arn
                    and p.get("State") not in _DEAD_POOLS), key=lambda p: p["IpamPoolId"])
    for pool in pools:
        pid, tags = pool["IpamPoolId"], _tags(pool.get("Tags"))
        tag_keys |= set(tags)
        name = tags.get("Name") or pool.get("Description") or ""
        cidrs = _call(f"read the CIDRs of pool {pid}", _all, client, "get_ipam_pool_cidrs",
                      "IpamPoolCidrs", IpamPoolId=pid)
        for cidr in sorted((c for c in cidrs if c.get("State") in _LIVE_POOL_CIDRS),
                           key=lambda c: c.get("Cidr", "")):
            rows.append(dict(_row(**{
                "Resource ID": pid, "Resource name": name, "Resource type": "pool",
                "Resource CIDR": cidr.get("Cidr"), "Region": pool.get("Locale"),
                "Resource owner": pool.get("OwnerId"), "IPAM pool ID": pool.get("SourceIpamPoolId"),
                "Description": pool.get("Description"), "Management state": "managed"}),
                **{f"tag:{k}": v for k, v in tags.items()}))
        allocations = _call(f"read the allocations of pool {pid}", _all, client,
                            "get_ipam_pool_allocations", "IpamPoolAllocations", IpamPoolId=pid)
        for allocation in sorted((a for a in allocations if a.get("ResourceType") == "custom"),
                                 key=lambda a: a.get("Cidr", "")):
            atags = _tags(allocation.get("Tags"))
            tag_keys |= set(atags)
            rows.append(dict(_row(**{
                "Resource ID": allocation.get("IpamPoolAllocationId"),
                "Resource name": atags.get("Name") or allocation.get("Description"),
                "Resource type": "allocation", "Resource CIDR": allocation.get("Cidr"),
                "Region": allocation.get("ResourceRegion") or pool.get("Locale"),
                "Resource owner": allocation.get("ResourceOwner") or pool.get("OwnerId"),
                "IPAM pool ID": pid, "Description": allocation.get("Description"),
                "Management state": "managed"}), **{f"tag:{k}": v for k, v in atags.items()}))

    resources = _call("read IPAM resource CIDRs", _all, client, "get_ipam_resource_cidrs",
                      "IpamResourceCidrs", IpamScopeId=scope_id)
    for resource in sorted(resources, key=lambda r: (r.get("ResourceType", ""),
                                                     r.get("ResourceId", ""),
                                                     r.get("ResourceCidr", ""))):
        rtags = _tags(resource.get("ResourceTags"))
        tag_keys |= set(rtags)
        usage = resource.get("IpUsage")
        rows.append(dict(_row(**{
            "Resource ID": resource.get("ResourceId"),
            "Resource name": resource.get("ResourceName") or rtags.get("Name"),
            "Resource type": resource.get("ResourceType"), "Resource CIDR": resource.get("ResourceCidr"),
            "Region": resource.get("ResourceRegion"), "Resource owner": resource.get("ResourceOwnerId"),
            "VPC ID": resource.get("VpcId"), "IPAM pool ID": resource.get("IpamPoolId"),
            "Management state": resource.get("ManagementState"),
            "Compliance status": resource.get("ComplianceStatus"),
            "Overlap status": resource.get("OverlapStatus"),
            "IP usage": f"{usage * 100:.0f}%" if isinstance(usage, (int, float)) else ""}),
            **{f"tag:{k}": v for k, v in rtags.items()}))

    headers = HEADERS + [f"tag:{k}" for k in sorted(tag_keys)]
    table = Table(f"AWS IPAM {ipam['IpamId']} ({client.meta.region_name}), scope {scope_id}", "",
                  1, headers)
    for number, row in enumerate(rows, start=2):  # row 1 is the header, as in --save-export
        table.rows.append((number, {h: row.get(h, "") for h in headers}))
    return table


def to_csv(table: Table) -> str:
    """The table as the CSV `--save-export` writes, which `plan` reads back."""
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(table.headers)
    for _, values in table.rows:
        writer.writerow([values.get(h, "") for h in table.headers])
    return out.getvalue()
