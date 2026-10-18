"""Collect an AWS Organization: its root, OUs, accounts, policies and
delegated administrators (read-only).

Run it with credentials for the management account, or for an account that
is a delegated administrator for AWS Organizations: only those can list the
whole organization. It shares the AWS collector's read-only client, so it
can only call Describe/List/Get operations (plus STS AssumeRole when
`--role-arn` is given).

Accounts get the same IDs the network collector gives them
(`aws:account:<id>`), so merging an organization snapshot with an `aws`
snapshot draws each account, with its VPCs, inside its OU.
"""

from __future__ import annotations

import argparse
from typing import Any, Dict, List, Optional, Tuple

from ..model import (AWS_ACCOUNT, AWS_ORG, AWS_ORG_POLICY, AWS_ORG_ROOT, AWS_OU, LINK_POLICY,
                     Topology, node_id)
from .aws import (FALLBACK_REGION, ReadOnlyClient, _boto_errors, _error_code, _label,
                  _new_client, _now, _paginate, _require_boto3, _role_arn)

HELP = ("Collect an AWS Organization: OUs, accounts, policies (SCPs, RCPs, tag, backup, ...) and "
        "delegated administrators. Run against the management account (read-only).")
PRODUCES_TOPOLOGY = True

EXTRAS = ("tags", "emails", "aws-managed-policies")

# Organization policy types, and the short names used in node attributes.
POLICY_TYPES = {
    "SERVICE_CONTROL_POLICY": "scp",
    "RESOURCE_CONTROL_POLICY": "rcp",
    "TAG_POLICY": "tag",
    "BACKUP_POLICY": "backup",
    "AISERVICES_OPT_OUT_POLICY": "ai-opt-out",
    "CHATBOT_POLICY": "chatbot",
    "DECLARATIVE_POLICY_EC2": "declarative-ec2",
}

_WHO_CAN = ("run topomap with credentials for the management account, or for an account that is "
            "a delegated administrator for AWS Organizations")


def _include_list(value: str) -> List[str]:
    items = [v.strip() for v in value.split(",") if v.strip()]
    unknown = [v for v in items if v not in EXTRAS]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown --include value {', '.join(unknown)} (choose from {', '.join(EXTRAS)})")
    return items


def add_arguments(parser) -> None:
    parser.add_argument("--profile", metavar="NAME",
                        help="AWS profile for the management account (default: boto3's default "
                             "credential chain)")
    parser.add_argument("--role-arn", type=_role_arn, metavar="ARN",
                        help="assume this role (in the management account) first")
    parser.add_argument("--external-id", help="external ID to pass when assuming the role")
    parser.add_argument("--role-session-name", default="topomap",
                        help="session name for the assumed role (default: topomap)")
    parser.add_argument("--policies", choices=("attrs", "nodes", "none"), default="attrs",
                        help="how to show organization policies: as attributes of the root, OUs "
                             "and accounts they're attached to (default), also as policy nodes "
                             "linked to their targets, or not at all")
    parser.add_argument("--include", action="extend", type=_include_list, default=[],
                        metavar="LIST",
                        help="comma-separated extras: tags (one call per account), emails (account "
                             "email addresses; personal data, off by default), aws-managed-policies "
                             "(FullAWSAccess and friends, which are attached everywhere)")


# -- credentials -------------------------------------------------------------------

def _organizations_client(boto3: Any, args: Any) -> ReadOnlyClient:
    from botocore.exceptions import ProfileNotFound
    where = f"profile {args.profile!r}" if args.profile else "the default credential chain"
    try:
        session = boto3.Session(profile_name=args.profile)
    except ProfileNotFound as e:
        raise RuntimeError(str(e)) from None
    if session.get_credentials() is None:
        raise RuntimeError(f"no AWS credentials found in {where}; run 'aws configure' or "
                           "'aws sso login', set AWS_PROFILE, or pass --profile")
    region = session.region_name or FALLBACK_REGION
    if args.role_arn:
        sts = ReadOnlyClient(_new_client(session, "sts", region), also=("assume_role",))
        kwargs = {"RoleArn": args.role_arn, "RoleSessionName": args.role_session_name}
        if args.external_id:
            kwargs["ExternalId"] = args.external_id
        try:
            creds = sts.assume_role(**kwargs)["Credentials"]
        except _boto_errors() as e:
            raise RuntimeError(f"cannot assume {args.role_arn} ({_error_code(e)})") from None
        session = boto3.Session(aws_access_key_id=creds["AccessKeyId"],
                                aws_secret_access_key=creds["SecretAccessKey"],
                                aws_session_token=creds["SessionToken"], region_name=region)
    return ReadOnlyClient(_new_client(session, "organizations", region))


# -- structure ---------------------------------------------------------------------

class _Org:
    """What has been collected so far, and where each target ID was drawn."""

    def __init__(self, client: Any, include: List[str]):
        self.client = client
        self.include = set(include)
        self.topo = Topology()
        self.targets: Dict[str, str] = {}   # root / OU / account ID -> node ID
        self.management = ""

    def warn(self, message: str) -> None:
        self.topo.warn(f"aws-org: {message}")


def _organization(org: _Org) -> str:
    try:
        info = org.client.describe_organization()["Organization"]
    except _boto_errors() as e:
        code = _error_code(e)
        if code == "AWSOrganizationsNotInUseException":
            raise RuntimeError("these credentials' account isn't part of an AWS Organization") from None
        raise RuntimeError(f"cannot read the organization ({code}): {_WHO_CAN}") from None
    org.management = info.get("MasterAccountId", "")
    oid = node_id("aws", AWS_ORG, info["Id"])
    org.topo.add_node(oid, AWS_ORG, f"Organization {info['Id']}", org_id=info["Id"],
                      arn=info.get("Arn"), feature_set=info.get("FeatureSet"),
                      management_account_id=org.management or None)
    return oid


def _roots(org: _Org, oid: str) -> List[Tuple[str, str, str]]:
    """(root ID, node ID, name) of each root. Listing roots is the first
    call only the management account or a delegated admin may make, so a
    denial here is fatal and says who to run as."""
    try:
        roots = _paginate(org.client, "list_roots", "Roots")
    except _boto_errors() as e:
        raise RuntimeError(f"cannot list the organization ({_error_code(e)}): {_WHO_CAN}") from None
    out = []
    for root in roots:
        rid = node_id("aws", AWS_ORG_ROOT, root["Id"])
        enabled = sorted(POLICY_TYPES.get(p["Type"], p["Type"].lower())
                         for p in root.get("PolicyTypes", []) if p.get("Status") == "ENABLED")
        name = root.get("Name") or "Root"
        org.topo.add_node(rid, AWS_ORG_ROOT, name, oid, root_id=root["Id"], arn=root.get("Arn"),
                          policy_types=enabled or None)
        org.targets[root["Id"]] = rid
        out.append((root["Id"], rid, name))
    return out


def _walk(org: _Org, root_id: str, root_node: str, root_name: str) -> None:
    """Breadth-first through the OU tree. A branch that can't be listed is
    a warning; the rest of the organization is still drawn."""
    queue = [(root_id, root_node, root_name)]
    while queue:
        parent_id, parent_node, path = queue.pop(0)
        try:
            ous = _paginate(org.client, "list_organizational_units_for_parent",
                            "OrganizationalUnits", ParentId=parent_id)
            accounts = _paginate(org.client, "list_accounts_for_parent", "Accounts",
                                 ParentId=parent_id)
        except _boto_errors() as e:
            org.warn(f"cannot list {path} ({_error_code(e)}); its contents are missing")
            continue
        for ou in ous:
            nid = node_id("aws", AWS_OU, ou["Id"])
            ou_path = f"{path}/{ou['Name']}"
            org.topo.add_node(nid, AWS_OU, ou["Name"], parent_node, ou_id=ou["Id"],
                              arn=ou.get("Arn"), path=ou_path)
            org.targets[ou["Id"]] = nid
            queue.append((ou["Id"], nid, ou_path))
        for account in accounts:
            _account(org, account, parent_node, path)


def _account(org: _Org, account: Dict[str, Any], parent_node: str, path: str) -> None:
    aid = account["Id"]
    nid = node_id("aws", AWS_ACCOUNT, aid)
    joined = account.get("JoinedTimestamp")
    org.topo.add_node(
        nid, AWS_ACCOUNT, _label({}, aid, account.get("Name", "")), parent_node,
        account_id=aid, name=account.get("Name") or None,
        status=account.get("State") or account.get("Status"),
        joined_method=account.get("JoinedMethod"),
        joined=joined.strftime("%Y-%m-%d") if hasattr(joined, "strftime") else None,
        management=True if aid == org.management else None,
        email=account.get("Email") if "emails" in org.include else None,
        path=path,
    )
    org.targets[aid] = nid


# -- extras ------------------------------------------------------------------------

def _delegated_admins(org: _Org) -> None:
    try:
        admins = _paginate(org.client, "list_delegated_administrators", "DelegatedAdministrators")
    except _boto_errors() as e:
        org.warn(f"cannot list delegated administrators ({_error_code(e)})")
        return
    for admin in admins:
        nid = org.targets.get(admin["Id"])
        if nid is None:
            continue
        try:
            services = _paginate(org.client, "list_delegated_services_for_account",
                                 "DelegatedServices", AccountId=admin["Id"])
        except _boto_errors() as e:
            org.warn(f"cannot list delegated services of {admin['Id']} ({_error_code(e)})")
            continue
        principals = sorted(s["ServicePrincipal"] for s in services)
        org.topo.nodes[nid].attrs["delegated_admin_for"] = principals


def _tags(org: _Org) -> None:
    for node in org.topo.by_kind(AWS_ACCOUNT):
        try:
            tags = _paginate(org.client, "list_tags_for_resource", "Tags",
                             ResourceId=node.attrs["account_id"])
        except _boto_errors() as e:
            org.warn(f"cannot read account tags ({_error_code(e)}); skipped the rest")
            return
        if tags:
            node.attrs["tags"] = {t["Key"]: t["Value"] for t in tags}


def _policies(org: _Org, oid: str, as_nodes: bool) -> None:
    """Record each policy on what it's attached to, as `policies: {type:
    [names]}`. Only types enabled on a root are listed; AWS-managed
    policies (FullAWSAccess, attached everywhere by default) are noise in a
    diagram and skipped unless asked for."""
    enabled = sorted({t for n in org.topo.by_kind(AWS_ORG_ROOT)
                      for t in n.attrs.get("policy_types") or []})
    by_short = {short: full for full, short in POLICY_TYPES.items()}
    for short in enabled:
        full = by_short.get(short, short.upper())
        try:
            policies = _paginate(org.client, "list_policies", "Policies", Filter=full)
        except _boto_errors() as e:
            org.warn(f"cannot list {short} policies ({_error_code(e)})")
            continue
        for policy in policies:
            if policy.get("AwsManaged") and "aws-managed-policies" not in org.include:
                continue
            try:
                targets = _paginate(org.client, "list_targets_for_policy", "Targets",
                                    PolicyId=policy["Id"])
            except _boto_errors() as e:
                org.warn(f"cannot list targets of policy {policy['Name']} ({_error_code(e)})")
                continue
            pid = node_id("aws", AWS_ORG_POLICY, policy["Id"])
            if as_nodes:
                org.topo.add_node(pid, AWS_ORG_POLICY, policy["Name"], oid,
                                  policy_id=policy["Id"], type=short,
                                  aws_managed=bool(policy.get("AwsManaged")),
                                  description=policy.get("Description") or None)
            for target in targets:
                nid = org.targets.get(target["TargetId"])
                if nid is None:
                    continue
                attached = org.topo.nodes[nid].attrs.setdefault("policies", {})
                names = attached.setdefault(short, [])
                if policy["Name"] not in names:
                    names.append(policy["Name"])
                    names.sort()
                if as_nodes:
                    org.topo.add_link(LINK_POLICY, pid, nid, directed=True, type=short)


# -- entry point -------------------------------------------------------------------

def collect(client: Any, include: List[str] = (), policies: str = "attrs") -> Topology:
    """The organization as a topology, from a (read-only) Organizations client."""
    org = _Org(client, list(include))
    oid = _organization(org)
    for root_id, root_node, name in _roots(org, oid):
        _walk(org, root_id, root_node, name)
    _delegated_admins(org)
    if "tags" in org.include:
        _tags(org)
    if policies != "none":
        _policies(org, oid, as_nodes=policies == "nodes")
    topo = org.topo
    for ou in topo.by_kind(AWS_OU):
        if not topo.children(ou.id):
            ou.attrs["empty"] = True
    meta: Dict[str, Any] = {
        "collector": "aws-org",
        "organization": topo.nodes[oid].attrs["org_id"],
        "management_account": org.management or None,
        "accounts": sorted(n.attrs["account_id"] for n in topo.by_kind(AWS_ACCOUNT)),
        "ous": sorted(n.attrs["ou_id"] for n in topo.by_kind(AWS_OU)),
        "collected_at": _now().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if topo.meta.get("warnings"):
        meta["warnings"] = topo.meta["warnings"]
    topo.meta = meta
    return topo


def run(args) -> Topology:
    boto3 = _require_boto3()
    client = _organizations_client(boto3, args)
    return collect(client, sorted(set(args.include or [])), args.policies)
