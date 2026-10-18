"""Collect VPCs, subnets, gateways, TGWs, VPNs, peering and Direct Connect from AWS (read-only).

The collector walks every (account, region) pair it can reach and turns what
it finds into the node and link kinds of DESIGN.md. Three things shape it:

* **Read-only, provably.** Every boto3 client is wrapped in `ReadOnlyClient`,
  which refuses any operation that isn't a Describe/List/Get/Search call, so
  a bug here can never change an account. The one exception is STS
  `AssumeRole`, which only mints credentials and is allowed on the STS client
  used to reach other accounts.
* **Partial failure is normal.** A denied call, an opted-out region or an
  unreachable endpoint becomes a `Topology.warn()` and collection carries on.
* **Deterministic output.** Each (account, region) is collected into its own
  small topology on a thread pool, and the pieces are merged in sorted order,
  so the same estate always produces the same JSON.

boto3 is imported lazily, so `topomap --help` works without it.
"""

from __future__ import annotations

import argparse
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from ..model import (AWS_ACCOUNT, AWS_AZ, AWS_CGW, AWS_DX_CONNECTION, AWS_DX_GATEWAY, AWS_EIGW,
                     AWS_ENDPOINT, AWS_IGW, AWS_INSTANCE, AWS_NAT, AWS_REGION, AWS_SUBNET, AWS_TGW,
                     AWS_VGW, AWS_VPC, LINK_DX_VIF, LINK_DXGW_ASSOCIATION, LINK_TGW_ATTACHMENT,
                     LINK_TGW_PEERING, LINK_VGW_ATTACHMENT, LINK_VPC_PEERING, LINK_VPN,
                     NET_EXTERNAL, Topology, node_id, union)

HELP = ("Collect VPCs, subnets, gateways, TGWs, VPNs, peering and Direct Connect from AWS "
        "(read-only), and with --ai, --iam and --eks Bedrock models, IAM roles and users, and "
        "EKS clusters.")
PRODUCES_TOPOLOGY = True

EXTRAS = ("instances", "endpoints", "tags")
READ_ONLY_PREFIXES = ("describe_", "list_", "get_", "search_")
FALLBACK_REGION = "us-east-1"

# The region to start from when nothing says which, per partition. GovCloud and
# China have their own endpoints and credentials, so the commercial default
# only works for commercial accounts.
PARTITION_HOMES = {"aws": "us-east-1", "aws-us-gov": "us-gov-west-1", "aws-cn": "cn-north-1"}


def partition_of_region(region: str) -> str:
    """The AWS partition a region name belongs to."""
    if region.startswith("us-gov-"):
        return "aws-us-gov"
    if region.startswith("cn-"):
        return "aws-cn"
    return "aws"


def fallback_region(region: str = "") -> str:
    """A region in the same partition as `region` (commercial if unknown)."""
    return PARTITION_HOMES[partition_of_region(region)] if region else FALLBACK_REGION


def probe_home(session: Any, preferred: Optional[str] = None) -> Tuple[str, Any]:
    """(home region, caller identity) for `session`.

    A region from --region or the profile is used as given. Without one the
    commercial, GovCloud and China starting points are tried in turn, since
    credentials only work in their own partition.
    """
    candidates = [preferred] if preferred else list(PARTITION_HOMES.values())
    error: Optional[Exception] = None
    for region in candidates:
        try:
            identity = ReadOnlyClient(_new_client(session, "sts", region)).get_caller_identity()
            return region, identity
        except _boto_errors() as e:
            error = e
    raise error  # type: ignore[misc]
PUBLIC_SERVICES = node_id("ext", NET_EXTERNAL, "aws-public")

# States meaning "this no longer exists, or never will": not drawn.
_GONE = frozenset({"deleted", "deleting", "disassociated", "disassociating", "detached",
                   "failed", "failing", "rejected", "expired", "terminated"})

# Errors that mean a whole region or service is out of reach, not one call.
_UNAVAILABLE = frozenset({"OptInRequired", "AuthFailure", "InvalidClientTokenId",
                          "UnrecognizedClientException", "ExpiredToken", "ExpiredTokenException",
                          "RequestExpired", "SubscriptionRequiredException"})

# Route targets, most specific first (a route to an instance also names its ENI).
_ROUTE_TARGETS = ("GatewayId", "NatGatewayId", "TransitGatewayId", "VpcPeeringConnectionId",
                  "EgressOnlyInternetGatewayId", "InstanceId", "NetworkInterfaceId",
                  "LocalGatewayId", "CarrierGatewayId", "CoreNetworkArn")

# Placeholder kinds for link ends that weren't collected, by the ID's middle part.
_PLACEHOLDER_KINDS = {"vpc": AWS_VPC, "tgw": AWS_TGW, "vgw": AWS_VGW, "cgw": AWS_CGW,
                      "dx-gateway": AWS_DX_GATEWAY, "dx-connection": AWS_DX_CONNECTION}

_ROLE_ARN = re.compile(r"^arn:[\w-]+:iam::(\d{12}):role/.+$")
_CLIENT_LOCK = threading.Lock()


# -- read-only clients ---------------------------------------------------------

class ReadOnlyError(RuntimeError):
    """An attempt to call an AWS operation that could change something."""


class ReadOnlyClient:
    """A boto3 client that can only read.

    Only operations named describe_*, list_*, get_* or search_* (plus
    pagination helpers and `meta`) are reachable; anything else raises
    `ReadOnlyError` before a request is built. `also` names extra operations
    allowed on this one client, which is how STS AssumeRole gets through.
    """

    def __init__(self, client: Any, also: Iterable[str] = ()):
        self._client = client
        self._also = frozenset(also)

    def _allowed(self, name: str) -> bool:
        return name.startswith(READ_ONLY_PREFIXES) or name in self._also

    def get_paginator(self, operation: str) -> Any:
        if not self._allowed(operation):
            raise ReadOnlyError(f"topomap is read-only: refusing to paginate {operation}")
        return self._client.get_paginator(operation)

    def can_paginate(self, operation: str) -> bool:
        return self._client.can_paginate(operation)

    def __getattr__(self, name: str) -> Any:
        if name in ("meta", "exceptions") or self._allowed(name):
            return getattr(self._client, name)
        raise ReadOnlyError(f"topomap is read-only: refusing to call {name}")


def _new_client(session: Any, service: str, region: str) -> Any:
    """A raw boto3 client with retries suited to walking a big estate.

    boto3 sessions aren't thread-safe, so clients are created under a lock.
    Tests replace this function to inject stubs.
    """
    from botocore.config import Config
    config = Config(retries={"mode": "standard", "max_attempts": 10}, user_agent_extra="topomap")
    with _CLIENT_LOCK:
        return session.client(service, region_name=region, config=config)


def _require_boto3() -> Any:
    try:
        import boto3
    except ImportError:
        raise RuntimeError("the AWS collector needs boto3: pip install 'topomap[aws]'") from None
    return boto3


def _error_code(error: Exception) -> str:
    response = getattr(error, "response", None) or {}
    return response.get("Error", {}).get("Code") or type(error).__name__


def _boto_errors() -> Tuple[type, ...]:
    from botocore.exceptions import BotoCoreError, ClientError
    return (BotoCoreError, ClientError)


def _paginate(client: Any, operation: str, key: str, **kwargs: Any) -> List[dict]:
    """Every item of a listing. Some calls (DescribeVpnConnections, Direct
    Connect's DescribeConnections) have no paginator and return everything
    at once."""
    if client.can_paginate(operation):
        items: List[dict] = []
        for page in client.get_paginator(operation).paginate(**kwargs):
            items.extend(page.get(key, []))
        return items
    return getattr(client, operation)(**kwargs).get(key, [])


# -- CLI -----------------------------------------------------------------------

def _include_list(value: str) -> List[str]:
    items = [v.strip() for v in value.split(",") if v.strip()]
    unknown = [v for v in items if v not in EXTRAS]
    if unknown:
        raise argparse.ArgumentTypeError(
            f"unknown --include value {', '.join(unknown)} (choose from {', '.join(EXTRAS)})")
    return items


def _role_arn(value: str) -> str:
    if not _ROLE_ARN.match(value):
        raise argparse.ArgumentTypeError(f"not an IAM role ARN: {value}")
    return value


def _positive_int(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = 0
    if number < 1:
        raise argparse.ArgumentTypeError(f"must be a positive integer: {value}")
    return number


def add_arguments(parser) -> None:
    parser.add_argument("--profile", action="append", metavar="NAME",
                        help="AWS profile to use; repeat for several (default: boto3's default "
                             "credential chain)")
    parser.add_argument("--region", action="append", metavar="NAME",
                        help="region to collect; repeat for several (default: every region "
                             "enabled for the account)")
    parser.add_argument("--role-arn", action="append", type=_role_arn, metavar="ARN",
                        help="assume this role from each profile and collect its account; "
                             "repeat for several. The profiles' own accounts are then not "
                             "collected unless --org-role-name lists them")
    parser.add_argument("--external-id", help="external ID to pass when assuming roles")
    parser.add_argument("--role-session-name", default="topomap",
                        help="session name for assumed roles (default: topomap)")
    parser.add_argument("--org-role-name", metavar="NAME",
                        help="list the organization's accounts and assume "
                             "arn:aws:iam::<id>:role/NAME in each (the caller's own account is "
                             "collected directly)")
    parser.add_argument("--include", action="extend", type=_include_list, default=[],
                        metavar="LIST",
                        help=f"comma-separated extras beyond the core: {', '.join(EXTRAS)}")
    parser.add_argument("--ai", action="store_true",
                        help="also collect the Bedrock models each account can use, per region "
                             "(foundation models it is authorized for, plus custom and imported "
                             "ones)")
    parser.add_argument("--iam", action="store_true",
                        help="also collect each account's custom IAM roles and users: trust, "
                             "policy names, MFA and access-key age (no credentials or policy "
                             "documents)")
    parser.add_argument("--iam-all", action="store_true",
                        help="with --iam, include service-linked and IAM Identity Center roles too")
    parser.add_argument("--eks", action="store_true",
                        help="also collect EKS clusters: version, endpoint access, node groups, "
                             "Fargate profiles and add-ons")
    parser.add_argument("--skip-network", action="store_true",
                        help="don't collect VPCs, gateways, TGWs, VPNs and Direct Connect; for "
                             "a faster run when you only want --ai, --iam or --eks")
    parser.add_argument("--workers", type=_positive_int, default=8, metavar="N",
                        help="(account, region) pairs collected in parallel (default: 8)")


# -- accounts and sessions -----------------------------------------------------

def _now() -> datetime:
    return datetime.now(timezone.utc)


class _Account:
    """One account to collect, and how to get read-only clients for it.

    An assumed role is re-assumed shortly before its credentials expire,
    because walking a large organization can outlast a one-hour role session.
    """

    def __init__(self, account_id: str, session: Any, home_region: str, name: str = "",
                 renew: Optional[Callable[[], Tuple[Any, datetime]]] = None,
                 expires: Optional[datetime] = None):
        self.account_id = account_id
        self.home_region = home_region
        self.name = name
        self._session = session
        self._renew = renew
        self._expires = expires
        self._lock = threading.Lock()

    def client(self, service: str, region: Optional[str] = None) -> ReadOnlyClient:
        with self._lock:
            if self._renew and self._expires and _now() > self._expires - timedelta(minutes=5):
                self._session, self._expires = self._renew()
            session = self._session
        return ReadOnlyClient(_new_client(session, service, region or self.home_region))


@dataclass
class _Base:
    """Credentials from a profile (or the default chain) and whose they are."""
    session: Any
    account_id: str
    partition: str
    home_region: str


def _base_sessions(boto3: Any, args: Any, warnings: List[str]) -> List[_Base]:
    from botocore.exceptions import ProfileNotFound
    bases = []
    for profile in args.profile or [None]:
        where = f"profile {profile!r}" if profile else "the default credential chain"
        try:
            session = boto3.Session(profile_name=profile)
        except ProfileNotFound as e:
            raise RuntimeError(str(e)) from None
        try:
            credentials = session.get_credentials()
        except _boto_errors() as e:
            warnings.append(f"aws: cannot load credentials from {where} ({_error_code(e)})")
            continue
        if credentials is None:
            raise RuntimeError(f"no AWS credentials found in {where}; run 'aws configure' or "
                               "'aws sso login', set AWS_PROFILE, or pass --profile")
        try:
            home, identity = probe_home(session, (args.region or [None])[0] or session.region_name)
        except _boto_errors() as e:
            warnings.append(f"aws: cannot use credentials from {where} ({_error_code(e)})")
            continue
        bases.append(_Base(session, identity["Account"], identity["Arn"].split(":")[1], home))
    return bases


def _org_accounts(base: _Base, warnings: List[str]) -> List[Tuple[str, str]]:
    """(account ID, name) of every active account in the organization. If
    the listing is denied, the caller's own account is still collected."""
    try:
        org = ReadOnlyClient(_new_client(base.session, "organizations", base.home_region))
        accounts = _paginate(org, "list_accounts", "Accounts")
    except _boto_errors() as e:
        warnings.append(f"aws {base.account_id}: cannot list organization accounts "
                        f"({_error_code(e)})")
        return [(base.account_id, "")]
    return [(a["Id"], a.get("Name", "")) for a in accounts
            if (a.get("State") or a.get("Status") or "ACTIVE") == "ACTIVE"]


# A plan is (base credentials, role ARN to assume or None for the base itself, account name).
_Plan = Tuple[_Base, Optional[str], str]


def _plans(bases: List[_Base], args: Any, warnings: List[str]) -> List[_Plan]:
    plans: List[_Plan] = []
    if not args.role_arn and not args.org_role_name:
        plans = [(b, None, "") for b in bases]
    for base in bases:
        plans.extend((base, arn, "") for arn in args.role_arn or [])
    if args.org_role_name:
        for base in bases:
            for account_id, name in _org_accounts(base, warnings):
                arn = None if account_id == base.account_id else (
                    f"arn:{base.partition}:iam::{account_id}:role/{args.org_role_name}")
                plans.append((base, arn, name))
    unique, seen = [], set()
    for plan in plans:
        account_id = _ROLE_ARN.match(plan[1]).group(1) if plan[1] else plan[0].account_id
        if account_id not in seen:
            seen.add(account_id)
            unique.append(plan)
    return unique


def _open_account(boto3: Any, plan: _Plan, args: Any) -> Tuple[Optional[_Account], str]:
    """The account a plan reaches, or a warning saying why it can't."""
    base, arn, name = plan
    if arn is None:
        return _Account(base.account_id, base.session, base.home_region, name), ""
    sts = ReadOnlyClient(_new_client(base.session, "sts", base.home_region), also=("assume_role",))

    def assume() -> Tuple[Any, datetime]:
        kwargs = {"RoleArn": arn, "RoleSessionName": args.role_session_name}
        if args.external_id:
            kwargs["ExternalId"] = args.external_id
        creds = sts.assume_role(**kwargs)["Credentials"]
        session = boto3.Session(aws_access_key_id=creds["AccessKeyId"],
                                aws_secret_access_key=creds["SecretAccessKey"],
                                aws_session_token=creds["SessionToken"],
                                region_name=base.home_region)
        return session, creds["Expiration"]

    try:
        session, expires = assume()
    except _boto_errors() as e:
        return None, f"aws: cannot assume {arn} ({_error_code(e)})"
    account_id = _ROLE_ARN.match(arn).group(1)
    return _Account(account_id, session, base.home_region, name, assume, expires), ""


def _accounts(boto3: Any, args: Any, pool: ThreadPoolExecutor,
              warnings: List[str]) -> List[_Account]:
    bases = _base_sessions(boto3, args, warnings)
    plans = _plans(bases, args, warnings)
    accounts = []
    for account, warning in pool.map(lambda p: _open_account(boto3, p, args), plans):
        if warning:
            warnings.append(warning)
        if account is not None:
            accounts.append(account)
    if not accounts:
        detail = "; ".join(warnings[:3]) or "nothing to collect"
        raise RuntimeError(f"no AWS account could be reached: {detail}")
    return accounts


# -- one unit of collection ----------------------------------------------------

class _Unavailable(Exception):
    """A service can't be used here at all; the rest of it is skipped."""


@dataclass
class _Scope:
    """What one (account, region) unit collects; region "" is the account-wide unit.

    `hints` holds attributes for things this unit links to but may not own
    (a peer VPC in another account): if nobody collects the real node, a
    placeholder marked `external` is drawn from them.
    """
    account_id: str
    region: str = ""
    topo: Topology = field(default_factory=Topology)
    hints: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    regions: List[str] = field(default_factory=list)
    region_source: str = ""
    scp_denied: List[str] = field(default_factory=list)
    grants: Any = None       # Bedrock grants from IAM, set on the account-wide unit

    @property
    def key(self) -> Tuple[str, str]:
        return (self.account_id, self.region)

    @property
    def account_node(self) -> str:
        return node_id("aws", "account", self.account_id)

    @property
    def region_node(self) -> str:
        return node_id("aws", "region", self.account_id, self.region)

    def warn(self, message: str) -> None:
        where = f"{self.account_id} {self.region}".strip()
        self.topo.warn(f"aws {where}: {message}")

    def hint(self, id: str, **attrs: Any) -> str:
        known = self.hints.setdefault(id, {})
        for key, value in attrs.items():
            if value not in (None, "", []):
                known.setdefault(key, value)
        return id

    def attempt(self, what: str, fn: Callable[..., Any], *args: Any, skip: str = "",
                **kwargs: Any) -> Any:
        """Run one collection step, turning AWS errors into warnings.

        `skip` marks the first call to a service: if that fails, the rest of
        the service is skipped with a single warning instead of one per call
        (an SCP that denies a region denies everything in it).
        """
        try:
            return fn(*args, **kwargs)
        except _boto_errors() as e:
            code = _error_code(e)
            if skip and _denied_by_scp(e):
                service = skip.split(" in ")[0]
                if service not in self.scp_denied:
                    self.scp_denied.append(service)
                    self.warn(f"{service} is blocked here by a service control policy")
                raise _Unavailable() from e
            if skip or code in _UNAVAILABLE:
                self.warn(f"cannot {what} ({code}); skipped {skip or 'the rest of it'}")
                raise _Unavailable() from e
            self.warn(f"cannot {what} ({code})")
        return None


def _denied_by_scp(error: Exception) -> bool:
    """An explicit deny from a service control policy: the account can't use
    the service here at all, whatever its IAM policies say."""
    response = getattr(error, "response", None) or {}
    message = str(response.get("Error", {}).get("Message") or error)
    return "service control policy" in message.lower()


def _aws(kind: str, resource_id: str) -> str:
    return node_id("aws", kind, resource_id)


def _tags(resource: dict) -> Dict[str, str]:
    """Tags as a dict. EC2 spells them Key/Value, Direct Connect key/value."""
    tags = {}
    for tag in resource.get("Tags") or resource.get("tags") or []:
        key = tag.get("Key", tag.get("key"))
        if key is not None:
            tags[key] = tag.get("Value", tag.get("value", ""))
    return dict(sorted(tags.items()))


def _label(tags: Dict[str, str], resource_id: str, name: str = "") -> str:
    """What a person calls it: "prod (vpc-0abc)", or just the ID."""
    name = (tags.get("Name") or name or "").strip()
    return f"{name} ({resource_id})" if name else resource_id


def _asn(value: Any) -> Any:
    """ASNs as integers; EC2 returns a customer gateway's as a string."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return value or None


def _live(items: Iterable[dict], state_key: str = "State") -> List[dict]:
    out = []
    for item in items:
        state = item.get(state_key)
        if isinstance(state, dict):
            state = state.get("State") or state.get("Name") or state.get("Code")
        if state not in _GONE:
            out.append(item)
    return out


# -- EC2: VPCs, subnets and gateways -------------------------------------------

def _ensure_vpc(scope: _Scope, vpc_id: str) -> str:
    """The VPC's node, adding a bare one if the VPC itself wasn't listed,
    so a child always has a parent."""
    nid = _aws("vpc", vpc_id)
    if nid not in scope.topo.nodes:
        scope.topo.add_node(nid, AWS_VPC, None, scope.region_node, vpc_id=vpc_id)
    return nid


def _parent_in_subnet(scope: _Scope, subnet_id: Optional[str], vpc_id: Optional[str]) -> str:
    sid = _aws("subnet", subnet_id) if subnet_id else ""
    if sid in scope.topo.nodes:
        return sid
    return _ensure_vpc(scope, vpc_id) if vpc_id else scope.region_node


def _vpcs(scope: _Scope, ec2: Any) -> None:
    for vpc in _paginate(ec2, "describe_vpcs", "Vpcs"):
        vid, tags = vpc["VpcId"], _tags(vpc)
        owner = vpc.get("OwnerId") or scope.account_id
        cidrs = [a["CidrBlock"] for a in _live(vpc.get("CidrBlockAssociationSet", []),
                                               "CidrBlockState")]
        ipv6 = [a["Ipv6CidrBlock"] for a in _live(vpc.get("Ipv6CidrBlockAssociationSet", []),
                                                  "Ipv6CidrBlockState") if a.get("Ipv6CidrBlock")]
        # A VPC shared through RAM is labelled by its owner, whose tags a
        # participant can't see.
        label = _label(tags, vid) if owner == scope.account_id else None
        scope.topo.add_node(_aws("vpc", vid), AWS_VPC, label, scope.region_node, vpc_id=vid,
                            cidrs=cidrs or [vpc.get("CidrBlock")], ipv6_cidrs=ipv6 or None,
                            is_default=bool(vpc.get("IsDefault")), account_id=owner,
                            region=scope.region, tags=tags or None)


@dataclass
class _RouteIndex:
    """Which route table applies to a subnet: its explicit association, or
    else its VPC's main route table."""
    by_subnet: Dict[str, dict] = field(default_factory=dict)
    main_by_vpc: Dict[str, dict] = field(default_factory=dict)

    def table_for(self, subnet_id: str, vpc_id: str) -> Optional[dict]:
        return self.by_subnet.get(subnet_id) or self.main_by_vpc.get(vpc_id)


def _route_index(ec2: Any) -> _RouteIndex:
    index = _RouteIndex()
    for table in _paginate(ec2, "describe_route_tables", "RouteTables"):
        for assoc in _live(table.get("Associations", []), "AssociationState"):
            if assoc.get("Main"):
                index.main_by_vpc[table["VpcId"]] = table
            elif assoc.get("SubnetId"):
                index.by_subnet[assoc["SubnetId"]] = table
    return index


def _routes(table: dict) -> List[Dict[str, str]]:
    routes = []
    for r in table.get("Routes", []):
        destination = (r.get("DestinationCidrBlock") or r.get("DestinationIpv6CidrBlock")
                       or r.get("DestinationPrefixListId") or "")
        target = next((r[k] for k in _ROUTE_TARGETS if r.get(k)), "")
        route = {"destination": destination, "target": target}
        if r.get("State") == "blackhole":
            route["state"] = "blackhole"
        routes.append(route)
    return sorted(routes, key=lambda r: (r["destination"], r["target"]))


def _is_public(table: dict) -> bool:
    return any(str(r.get("GatewayId", "")).startswith("igw-") and r.get("State") != "blackhole"
               for r in table.get("Routes", []))


def _subnets(scope: _Scope, ec2: Any, routes: Optional[_RouteIndex]) -> None:
    for subnet in _paginate(ec2, "describe_subnets", "Subnets"):
        sid, vid, az = subnet["SubnetId"], subnet["VpcId"], subnet["AvailabilityZone"]
        az_node = scope.topo.add_node(node_id("aws", "az", vid, az), AWS_AZ, az,
                                      _ensure_vpc(scope, vid), az=az,
                                      az_id=subnet.get("AvailabilityZoneId"))
        public = route_list = None
        if routes is not None:
            table = routes.table_for(sid, vid)
            public = _is_public(table) if table else False
            route_list = _routes(table) if table else []
        tags = _tags(subnet)
        ipv6 = [a["Ipv6CidrBlock"] for a in _live(subnet.get("Ipv6CidrBlockAssociationSet", []),
                                                  "Ipv6CidrBlockState")]
        scope.topo.add_node(_aws("subnet", sid), AWS_SUBNET, _label(tags, sid), az_node.id,
                            subnet_id=sid, cidr=subnet.get("CidrBlock"), ipv6_cidrs=ipv6 or None,
                            az=az, public=public, routes=route_list, tags=tags or None)


def _vpc_gateways(scope: _Scope, ec2: Any, operation: str, key: str, id_field: str,
                  short: str, kind: str) -> None:
    """Internet and egress-only gateways: drawn inside the VPC they're attached to."""
    for gateway in _paginate(ec2, operation, key):
        gid, tags = gateway[id_field], _tags(gateway)
        vpcs = [a["VpcId"] for a in _live(gateway.get("Attachments", []))]
        parent = _ensure_vpc(scope, vpcs[0]) if vpcs else scope.region_node
        scope.topo.add_node(_aws(short, gid), kind, _label(tags, gid), parent, gateway_id=gid,
                            tags=tags or None)


def _nat_gateways(scope: _Scope, ec2: Any) -> None:
    for nat in _live(_paginate(ec2, "describe_nat_gateways", "NatGateways")):
        nid, tags = nat["NatGatewayId"], _tags(nat)
        addresses = nat.get("NatGatewayAddresses", [])
        scope.topo.add_node(
            _aws("nat", nid), AWS_NAT, _label(tags, nid),
            _parent_in_subnet(scope, nat.get("SubnetId"), nat.get("VpcId")),
            nat_gateway_id=nid,
            public_ips=[a["PublicIp"] for a in addresses if a.get("PublicIp")] or None,
            private_ips=[a["PrivateIp"] for a in addresses if a.get("PrivateIp")] or None,
            connectivity=nat.get("ConnectivityType") or "public", tags=tags or None)


def _endpoints(scope: _Scope, ec2: Any) -> None:
    for endpoint in _live(_paginate(ec2, "describe_vpc_endpoints", "VpcEndpoints")):
        eid, tags = endpoint["VpcEndpointId"], _tags(endpoint)
        scope.topo.add_node(_aws("endpoint", eid), AWS_ENDPOINT, _label(tags, eid),
                            _ensure_vpc(scope, endpoint["VpcId"]), endpoint_id=eid,
                            service=endpoint.get("ServiceName"),
                            endpoint_type=endpoint.get("VpcEndpointType"), tags=tags or None)


def _instances(scope: _Scope, ec2: Any) -> None:
    for reservation in _paginate(ec2, "describe_instances", "Reservations"):
        for instance in _live(reservation.get("Instances", [])):
            iid, tags = instance["InstanceId"], _tags(instance)
            private_ips = [a["PrivateIpAddress"] for eni in instance.get("NetworkInterfaces", [])
                           for a in eni.get("PrivateIpAddresses", []) if a.get("PrivateIpAddress")]
            if not private_ips and instance.get("PrivateIpAddress"):
                private_ips = [instance["PrivateIpAddress"]]
            scope.topo.add_node(
                _aws("instance", iid), AWS_INSTANCE, _label(tags, iid),
                _parent_in_subnet(scope, instance.get("SubnetId"), instance.get("VpcId")),
                instance_id=iid, private_ips=private_ips or None,
                public_ip=instance.get("PublicIpAddress"),
                instance_type=instance.get("InstanceType"),
                state=(instance.get("State") or {}).get("Name"), tags=tags or None)


# -- EC2: transit gateways, peering and VPN ------------------------------------

def _tgw_routes(scope: _Scope, ec2: Any, table_id: str) -> List[Dict[str, Any]]:
    kwargs = {"TransitGatewayRouteTableId": table_id,
              "Filters": [{"Name": "state", "Values": ["active"]}]}
    if ec2.can_paginate("search_transit_gateway_routes"):
        found = _paginate(ec2, "search_transit_gateway_routes", "Routes", **kwargs)
    else:
        # Older botocore has no paginator here; AWS then returns at most 1000 routes.
        response = ec2.search_transit_gateway_routes(**kwargs)
        found = response.get("Routes", [])
        if response.get("AdditionalRoutesAvailable"):
            scope.warn(f"route table {table_id} has more routes than shown")
    routes = []
    for route in found:
        destination = route.get("DestinationCidrBlock") or route.get("PrefixListId") or ""
        for attachment in route.get("TransitGatewayAttachments") or [{}]:
            routes.append({"destination": destination,
                           "attachment": attachment.get("TransitGatewayAttachmentId", ""),
                           "type": route.get("Type", "")})
    return sorted(routes, key=lambda r: (r["destination"], r["attachment"]))


def _tgw_route_tables(scope: _Scope, ec2: Any) -> Dict[str, List[dict]]:
    """Route tables by transit gateway ID, each with its active routes."""
    tables: Dict[str, List[dict]] = {}
    for table in _live(_paginate(ec2, "describe_transit_gateway_route_tables",
                                 "TransitGatewayRouteTables")):
        table_id = table["TransitGatewayRouteTableId"]
        entry: Dict[str, Any] = {"id": table_id}
        routes = scope.attempt("search transit gateway routes", _tgw_routes, scope, ec2, table_id)
        if routes is not None:
            entry["routes"] = routes
        tables.setdefault(table["TransitGatewayId"], []).append(entry)
    return {tgw: sorted(entries, key=lambda e: e["id"]) for tgw, entries in tables.items()}


def _transit_gateways(scope: _Scope, ec2: Any, route_tables: Dict[str, List[dict]]) -> None:
    for tgw in _live(_paginate(ec2, "describe_transit_gateways", "TransitGateways")):
        tid, tags = tgw["TransitGatewayId"], _tags(tgw)
        owner = tgw.get("OwnerId") or scope.account_id
        label = _label(tags, tid) if owner == scope.account_id else None
        scope.topo.add_node(_aws("tgw", tid), AWS_TGW, label, scope.region_node, tgw_id=tid,
                            asn=(tgw.get("Options") or {}).get("AmazonSideAsn"),
                            account_id=owner, region=scope.region,
                            route_tables=route_tables.get(tid), tags=tags or None)


def _tgw_end(scope: _Scope, tgw_id: str, owner: Optional[str], region: Optional[str]) -> str:
    return scope.hint(_aws("tgw", tgw_id), tgw_id=tgw_id, account_id=owner,
                      region=region or scope.region)


def _attachment_end(scope: _Scope, attachment: dict) -> Optional[str]:
    """The node at the far end of a VPC or Direct Connect gateway attachment."""
    rtype, rid = attachment.get("ResourceType"), attachment.get("ResourceId", "")
    owner = attachment.get("ResourceOwnerId")
    if rtype == "vpc":
        return scope.hint(_aws("vpc", rid), vpc_id=rid, account_id=owner, region=scope.region)
    if rtype == "direct-connect-gateway":
        return scope.hint(_aws("dx-gateway", rid), dxgw_id=rid, account_id=owner)
    return None


def _tgw_attachments(scope: _Scope, ec2: Any) -> Dict[str, dict]:
    """Draw VPC, Direct Connect gateway and Connect attachments, and return
    every live attachment by ID so VPN links can name theirs. Peering and
    VPN attachments are drawn by `_tgw_peerings` and `_vpn_connections`."""
    attachments = {a["TransitGatewayAttachmentId"]: a for a in _live(_paginate(
        ec2, "describe_transit_gateway_attachments", "TransitGatewayAttachments"))}
    transports: Dict[str, str] = {}
    if any(a.get("ResourceType") == "connect" for a in attachments.values()):
        connects = _paginate(ec2, "describe_transit_gateway_connects", "TransitGatewayConnects")
        transports = {c["TransitGatewayAttachmentId"]:
                      c.get("TransportTransitGatewayAttachmentId", "") for c in connects}
    for aid, attachment in sorted(attachments.items()):
        rtype = attachment.get("ResourceType")
        # A Connect attachment rides on a VPC or DX transport: draw it there.
        carrier = attachments.get(transports.get(aid, "")) if rtype == "connect" else attachment
        end = _attachment_end(scope, carrier) if carrier else None
        if end is None:
            continue
        tgw = _tgw_end(scope, attachment["TransitGatewayId"],
                       attachment.get("TransitGatewayOwnerId"), scope.region)
        scope.topo.add_link(LINK_TGW_ATTACHMENT, end, tgw, key=aid,
                            label=_label(_tags(attachment), aid), attachment_id=aid,
                            state=attachment.get("State"), resource_type=rtype)
    return attachments


def _tgw_peerings(scope: _Scope, ec2: Any) -> None:
    for peering in _live(_paginate(ec2, "describe_transit_gateway_peering_attachments",
                                   "TransitGatewayPeeringAttachments")):
        ends = [peering.get("RequesterTgwInfo"), peering.get("AccepterTgwInfo")]
        if not all(end and end.get("TransitGatewayId") for end in ends):
            continue
        aid = peering["TransitGatewayAttachmentId"]
        a, b = (_tgw_end(scope, e["TransitGatewayId"], e.get("OwnerId"), e.get("Region"))
                for e in ends)
        scope.topo.add_link(LINK_TGW_PEERING, a, b, key=aid, label=_label(_tags(peering), aid),
                            attachment_id=aid,
                            state=(peering.get("Status") or {}).get("Code") or peering.get("State"))


def _vpc_peerings(scope: _Scope, ec2: Any) -> None:
    for pcx in _live(_paginate(ec2, "describe_vpc_peering_connections", "VpcPeeringConnections"),
                     "Status"):
        ends = []
        for info in (pcx.get("RequesterVpcInfo") or {}, pcx.get("AccepterVpcInfo") or {}):
            vid = info.get("VpcId")
            if not vid:
                break
            cidrs = [c["CidrBlock"] for c in info.get("CidrBlockSet", []) if c.get("CidrBlock")]
            if not cidrs and info.get("CidrBlock"):
                cidrs = [info["CidrBlock"]]
            ends.append(scope.hint(_aws("vpc", vid), vpc_id=vid, cidrs=cidrs,
                                   account_id=info.get("OwnerId"), region=info.get("Region")))
        if len(ends) != 2:
            continue
        pid = pcx["VpcPeeringConnectionId"]
        scope.topo.add_link(LINK_VPC_PEERING, ends[0], ends[1], key=pid,
                            label=_label(_tags(pcx), pid), peering_id=pid,
                            state=(pcx.get("Status") or {}).get("Code"))


def _vpn_gateways(scope: _Scope, ec2: Any) -> None:
    for vgw in _live(_paginate(ec2, "describe_vpn_gateways", "VpnGateways")):
        gid, tags = vgw["VpnGatewayId"], _tags(vgw)
        attached = _live(vgw.get("VpcAttachments", []))
        parent = _ensure_vpc(scope, attached[0]["VpcId"]) if attached else scope.region_node
        node = scope.topo.add_node(_aws("vgw", gid), AWS_VGW, _label(tags, gid), parent,
                                   vgw_id=gid, asn=vgw.get("AmazonSideAsn"), tags=tags or None)
        for attachment in attached:
            scope.topo.add_link(LINK_VGW_ATTACHMENT, node.id,
                                _ensure_vpc(scope, attachment["VpcId"]),
                                state=attachment.get("State"))


def _customer_gateways(scope: _Scope, ec2: Any) -> None:
    for cgw in _live(_paginate(ec2, "describe_customer_gateways", "CustomerGateways")):
        cid, tags = cgw["CustomerGatewayId"], _tags(cgw)
        scope.topo.add_node(_aws("cgw", cid), AWS_CGW, _label(tags, cid), scope.region_node,
                            cgw_id=cid, ip=cgw.get("IpAddress"), asn=_asn(cgw.get("BgpAsn")),
                            device_name=cgw.get("DeviceName"), tags=tags or None)


def _tunnels(vpn: dict) -> List[Dict[str, str]]:
    """Tunnel outside IPs, inside CIDRs and status. Pre-shared keys and the
    customer gateway configuration are deliberately never read out."""
    options = (vpn.get("Options") or {}).get("TunnelOptions", [])
    inside = {t.get("OutsideIpAddress"): t.get("TunnelInsideCidr") for t in options}
    telemetry = vpn.get("VgwTelemetry") or [{"OutsideIpAddress": t.get("OutsideIpAddress")}
                                           for t in options]
    tunnels = []
    for t in telemetry:
        ip = t.get("OutsideIpAddress")
        tunnel = {"outside_ip": ip, "inside_cidr": inside.get(ip), "status": t.get("Status")}
        tunnels.append({k: v for k, v in tunnel.items() if v})
    return sorted(tunnels, key=lambda t: t.get("outside_ip", ""))


def _vpn_connections(scope: _Scope, ec2: Any, attachments: Dict[str, dict]) -> None:
    by_vpn = {a.get("ResourceId"): a["TransitGatewayAttachmentId"]
              for a in attachments.values() if a.get("ResourceType") == "vpn"}
    for vpn in _live(_paginate(ec2, "describe_vpn_connections", "VpnConnections")):
        vid, tags = vpn["VpnConnectionId"], _tags(vpn)
        if vpn.get("TransitGatewayId"):
            gateway = _tgw_end(scope, vpn["TransitGatewayId"], None, scope.region)
        elif vpn.get("VpnGatewayId"):
            gateway = scope.hint(_aws("vgw", vpn["VpnGatewayId"]), vgw_id=vpn["VpnGatewayId"])
        else:
            continue  # e.g. attached to a Cloud WAN core network, which isn't modelled
        cgw = scope.hint(_aws("cgw", vpn["CustomerGatewayId"]), cgw_id=vpn["CustomerGatewayId"])
        static = (vpn.get("Options") or {}).get("StaticRoutesOnly")
        scope.topo.add_link(LINK_VPN, cgw, gateway, key=vid, label=_label(tags, vid), vpn_id=vid,
                            state=vpn.get("State"), routing="static" if static else "bgp",
                            tunnels=_tunnels(vpn) or None, attachment_id=by_vpn.get(vid))


def _collect_ec2(scope: _Scope, ec2: Any, include: Iterable[str]) -> None:
    scope.attempt("describe VPCs", _vpcs, scope, ec2, skip="EC2 in this region")
    routes = scope.attempt("describe route tables", _route_index, ec2)
    scope.attempt("describe subnets", _subnets, scope, ec2, routes)
    scope.attempt("describe internet gateways", _vpc_gateways, scope, ec2,
                  "describe_internet_gateways", "InternetGateways", "InternetGatewayId",
                  "igw", AWS_IGW)
    scope.attempt("describe egress-only internet gateways", _vpc_gateways, scope, ec2,
                  "describe_egress_only_internet_gateways", "EgressOnlyInternetGateways",
                  "EgressOnlyInternetGatewayId", "eigw", AWS_EIGW)
    scope.attempt("describe NAT gateways", _nat_gateways, scope, ec2)
    if "endpoints" in include:
        scope.attempt("describe VPC endpoints", _endpoints, scope, ec2)
    if "instances" in include:
        scope.attempt("describe instances", _instances, scope, ec2)
    tables = scope.attempt("describe transit gateway route tables", _tgw_route_tables, scope, ec2)
    scope.attempt("describe transit gateways", _transit_gateways, scope, ec2, tables or {})
    attachments = scope.attempt("describe transit gateway attachments", _tgw_attachments,
                                scope, ec2)
    scope.attempt("describe transit gateway peerings", _tgw_peerings, scope, ec2)
    scope.attempt("describe VPC peerings", _vpc_peerings, scope, ec2)
    scope.attempt("describe VPN gateways", _vpn_gateways, scope, ec2)
    scope.attempt("describe customer gateways", _customer_gateways, scope, ec2)
    scope.attempt("describe VPN connections", _vpn_connections, scope, ec2, attachments or {})


# -- Direct Connect ------------------------------------------------------------

def _dx_connections(scope: _Scope, dx: Any) -> None:
    for conn in _live(_paginate(dx, "describe_connections", "connections"), "connectionState"):
        cid, tags = conn["connectionId"], _tags(conn)
        scope.topo.add_node(_aws("dx-connection", cid), AWS_DX_CONNECTION,
                            _label(tags, cid, conn.get("connectionName", "")), scope.region_node,
                            connection_id=cid, location=conn.get("location"),
                            bandwidth=conn.get("bandwidth"), state=conn.get("connectionState"),
                            tags=tags or None)


def _vif_target(scope: _Scope, vif: dict) -> str:
    if vif.get("directConnectGatewayId"):
        gid = vif["directConnectGatewayId"]
        return scope.hint(_aws("dx-gateway", gid), dxgw_id=gid)
    if vif.get("virtualGatewayId"):
        gid = vif["virtualGatewayId"]
        return scope.hint(_aws("vgw", gid), vgw_id=gid, account_id=vif.get("ownerAccount"),
                          region=vif.get("region") or scope.region)
    # A public VIF reaches AWS's public endpoints rather than a gateway.
    scope.topo.add_node(PUBLIC_SERVICES, NET_EXTERNAL, "AWS public services")
    return PUBLIC_SERVICES


def _dx_vifs(scope: _Scope, dx: Any) -> None:
    for vif in _live(_paginate(dx, "describe_virtual_interfaces", "virtualInterfaces"),
                     "virtualInterfaceState"):
        vid = vif["virtualInterfaceId"]
        connection = scope.hint(_aws("dx-connection", vif["connectionId"]),
                                connection_id=vif["connectionId"], region=scope.region)
        scope.topo.add_link(
            LINK_DX_VIF, connection, _vif_target(scope, vif), directed=True, key=vid,
            label=_label(_tags(vif), vid, vif.get("virtualInterfaceName", "")), vif_id=vid,
            vif_type=vif.get("virtualInterfaceType"), vlan=vif.get("vlan"),
            customer_address=vif.get("customerAddress"), amazon_address=vif.get("amazonAddress"),
            asn=vif.get("asn"), amazon_asn=vif.get("amazonSideAsn"),
            state=vif.get("virtualInterfaceState"))


def _collect_dx(scope: _Scope, dx: Any) -> None:
    scope.attempt("describe Direct Connect connections", _dx_connections, scope, dx,
                  skip="Direct Connect in this region")
    scope.attempt("describe virtual interfaces", _dx_vifs, scope, dx)


_ASSOCIATED_KINDS = {"transitGateway": ("tgw", "tgw_id"),
                     "virtualPrivateGateway": ("vgw", "vgw_id")}


def _dxgw_associations(scope: _Scope, dx: Any, dxgw: str, gateway_id: str) -> None:
    for assoc in _live(_paginate(dx, "describe_direct_connect_gateway_associations",
                                 "directConnectGatewayAssociations",
                                 directConnectGatewayId=gateway_id), "associationState"):
        gateway = assoc.get("associatedGateway") or {}
        short, attr = _ASSOCIATED_KINDS.get(gateway.get("type", ""), ("vgw", "vgw_id"))
        target_id = gateway.get("id") or assoc.get("virtualGatewayId")
        if not target_id:
            continue
        owner = gateway.get("ownerAccount") or assoc.get("virtualGatewayOwnerAccount")
        region = gateway.get("region") or assoc.get("virtualGatewayRegion")
        target = scope.hint(_aws(short, target_id), **{attr: target_id}, account_id=owner,
                            region=region)
        prefixes = [p["cidr"] for p in assoc.get("allowedPrefixesToDirectConnectGateway", [])
                    if p.get("cidr")]
        aid = assoc.get("associationId", "")
        scope.topo.add_link(LINK_DXGW_ASSOCIATION, dxgw, target, key=aid, association_id=aid,
                            state=assoc.get("associationState"),
                            allowed_prefixes=sorted(prefixes) or None)


def _dxgw_attachments(dx: Any, gateway_id: str) -> List[Dict[str, str]]:
    """The VIFs attached to a DX gateway. The VIFs themselves (and their
    links) come from the regions; this list also names VIFs owned by
    accounts that weren't collected."""
    attachments = _live(_paginate(dx, "describe_direct_connect_gateway_attachments",
                                  "directConnectGatewayAttachments",
                                  directConnectGatewayId=gateway_id), "attachmentState")
    found = [{"vif_id": a.get("virtualInterfaceId"), "region": a.get("virtualInterfaceRegion"),
              "account_id": a.get("virtualInterfaceOwnerAccount"),
              "state": a.get("attachmentState")} for a in attachments]
    return sorted(({k: v for k, v in a.items() if v} for a in found),
                  key=lambda a: a.get("vif_id", ""))


def _dx_gateways(scope: _Scope, dx: Any) -> None:
    gateways = scope.attempt("describe Direct Connect gateways", _paginate, dx,
                             "describe_direct_connect_gateways", "directConnectGateways",
                             skip="Direct Connect gateways")
    for gateway in _live(gateways or [], "directConnectGatewayState"):
        gid = gateway["directConnectGatewayId"]
        node = scope.topo.add_node(
            _aws("dx-gateway", gid), AWS_DX_GATEWAY,
            _label({}, gid, gateway.get("directConnectGatewayName", "")), scope.account_node,
            dxgw_id=gid, asn=gateway.get("amazonSideAsn"),
            account_id=gateway.get("ownerAccount") or scope.account_id)
        scope.attempt("describe Direct Connect gateway associations", _dxgw_associations,
                      scope, dx, node.id, gid)
        attachments = scope.attempt("describe Direct Connect gateway attachments",
                                    _dxgw_attachments, dx, gid)
        if attachments:
            node.attrs["vif_attachments"] = attachments


# -- units -----------------------------------------------------------------------

def _account_name(account: _Account) -> str:
    """Organizations knows the account's name, but only the management
    account (or a delegated admin) may ask; the IAM alias is the usual
    fallback. Both are cosmetic, so a denial is silently ignored."""
    try:
        org = account.client("organizations")
        return org.describe_account(AccountId=account.account_id)["Account"].get("Name", "")
    except _boto_errors():
        pass
    try:
        aliases = account.client("iam").list_account_aliases().get("AccountAliases", [])
        return aliases[0] if aliases else ""
    except _boto_errors():
        return ""


def _enabled_regions(scope: _Scope, account: _Account) -> List[str]:
    try:
        regions = account.client("ec2").describe_regions().get("Regions", [])
    except _boto_errors() as e:
        home = fallback_region(account.home_region)
        scope.warn(f"cannot list enabled regions ({_error_code(e)}); collecting {home} only")
        scope.region_source = "fallback"
        return [home]
    scope.region_source = "describe-regions"
    return sorted(r["RegionName"] for r in regions) or [fallback_region(account.home_region)]


def _wanted(args: Any) -> frozenset:
    """The optional collections asked for, as the names `include` carries."""
    flags = {"ai": "ai", "iam": "iam", "iam_all": "iam-all", "eks": "eks",
             "skip_network": "skip-network"}
    return frozenset(name for attr, name in flags.items() if getattr(args, attr, False))


def _needs_regions(include: Iterable[str]) -> bool:
    include = set(include)
    return "skip-network" not in include or bool(include & {"ai", "eks"})


def _collect_account(account: _Account, regions: Optional[List[str]],
                     include: Iterable[str] = ()) -> _Scope:
    """The account-wide unit: the account node, its regions to walk, and
    Direct Connect gateways, which are global and so listed once."""
    scope = _Scope(account.account_id)
    name = account.name or _account_name(account)
    scope.topo.add_node(scope.account_node, AWS_ACCOUNT, _label({}, account.account_id, name),
                        account_id=account.account_id, name=name or None, collected=True)
    include = set(include)
    if not _needs_regions(include):
        scope.regions = []
    elif regions:
        scope.region_source = "--region"
        scope.regions = list(dict.fromkeys(regions))
    else:
        scope.regions = _enabled_regions(scope, account)
    if "skip-network" not in include:
        _guarded(scope, "directconnect",
                 lambda: _dx_gateways(scope, account.client("directconnect")))
    if "iam" in include:
        from . import aws_iam
        _guarded(scope, "iam", lambda: aws_iam.collect(scope, account.client("iam"),
                                                       "iam-all" in include))
    if "ai" in include:
        from . import aws_ai
        _guarded(scope, "bedrock-grants", lambda: setattr(
            scope, "grants", aws_ai.collect_grants(scope, account.client("iam"))))
    return scope


def _collect_region(account: _Account, region: str, include: Iterable[str],
                    grants: Any = None) -> _Scope:
    scope = _Scope(account.account_id, region)
    scope.topo.add_node(scope.account_node, AWS_ACCOUNT, account_id=account.account_id)
    scope.topo.add_node(scope.region_node, AWS_REGION, region, scope.account_node, region=region)
    if "skip-network" not in include:
        _guarded(scope, "ec2", lambda: _collect_ec2(scope, account.client("ec2", region), include))
        _guarded(scope, "directconnect",
                 lambda: _collect_dx(scope, account.client("directconnect", region)))
    if "eks" in include:
        from . import aws_eks
        _guarded(scope, "eks", lambda: aws_eks.collect(scope, account.client("eks", region)))
    if "ai" in include:
        from . import aws_ai
        _guarded(scope, "bedrock", lambda: aws_ai.collect(scope, account, region, grants))
    if scope.scp_denied:
        scope.topo.add_node(scope.region_node, AWS_REGION, region, scope.account_node,
                            scp_denied=sorted(scope.scp_denied))
    return scope


def _guarded(scope: _Scope, service: str, collect: Callable[[], None]) -> None:
    """Run one service's collection so that nothing it raises can sink the
    others: an unexpected response shape or SDK quirk becomes a warning.
    A read-only refusal is a bug in topomap, so that one still propagates."""
    try:
        collect()
    except _Unavailable:
        pass
    except ReadOnlyError:
        raise
    except Exception as e:  # noqa: BLE001 - see docstring
        scope.warn(f"{service}: skipped after unexpected {type(e).__name__}: {e}")


# -- assembly --------------------------------------------------------------------

def _add_placeholders(topo: Topology, hints: Dict[str, Dict[str, Any]]) -> None:
    """Give every link end that nobody collected a node marked `external`,
    so a peering to another account still has somewhere to go."""
    for link in sorted(topo.links.values(), key=lambda l: l.id):
        for end in (link.source, link.target):
            if end in topo.nodes:
                continue
            _, short, rest = end.split(":", 2)
            topo.add_node(end, _PLACEHOLDER_KINDS.get(short, NET_EXTERNAL), rest, external=True,
                          **hints.get(end, {}))


def _reparent_shared(topo: Topology) -> None:
    """A VPC or TGW shared through RAM is seen by every participant; draw it
    in its owner's region whenever the owner was collected too."""
    for node in topo.by_kind(AWS_VPC, AWS_TGW):
        owner, region = node.attrs.get("account_id"), node.attrs.get("region")
        home = node_id("aws", "region", owner, region)
        if node.parent and owner and region and home in topo.nodes:
            node.parent = home


def _mark_empty_regions(topo: Topology) -> None:
    """Flag regions that were reached but hold nothing, so a diagram can say
    "no resources" instead of leaving a bare box that looks unscanned."""
    for node in topo.by_kind(AWS_REGION):
        if not topo.children(node.id):
            node.attrs["empty"] = True


def _drop_empty_regions(topo: Topology) -> None:
    """Without the network collection an empty region only means "nothing
    of what was asked for", which every region without Bedrock or EKS would
    say; leave them off rather than draw dozens of empty boxes."""
    for node in topo.by_kind(AWS_REGION):
        if node.attrs.get("empty") is True:
            del topo.nodes[node.id]


def _add_trust_links(topo: Topology) -> None:
    from . import aws_iam
    aws_iam.add_trust_links(topo)


def _fill_labels(topo: Topology) -> None:
    """Nodes only ever seen without a label get their resource ID."""
    for node in topo.nodes.values():
        if node.label == node.id:
            node.label = node.id.split(":")[-1]


def _region_source(scopes: List[_Scope]) -> str:
    """How the regions were chosen, or "mixed" when accounts differ (one
    account's region listing may be denied while the others' is not)."""
    sources = {s.region_source for s in scopes if s.region_source}
    return sources.pop() if len(sources) == 1 else "mixed" if sources else ""


def _drop_tags(topo: Topology) -> None:
    """Tags are free text people fill in (project names, owners); they are
    only kept when asked for. The Name tag still labels the node."""
    for node in topo.nodes.values():
        node.attrs.pop("tags", None)
    for link in topo.links.values():
        link.attrs.pop("tags", None)


def _assemble(scopes: List[_Scope], warnings: List[str], accounts: List[str],
              regions: List[str], include: Iterable[str] = ()) -> Topology:
    first = Topology()
    for warning in warnings:
        first.warn(warning)
    ordered = sorted(scopes, key=lambda s: s.key)
    topo = union([first] + [s.topo for s in ordered])
    hints: Dict[str, Dict[str, Any]] = {}
    for scope in ordered:
        for nid, attrs in scope.hints.items():
            known = hints.setdefault(nid, {})
            for key, value in attrs.items():
                known.setdefault(key, value)
    _add_placeholders(topo, hints)
    _reparent_shared(topo)
    _mark_empty_regions(topo)
    if "skip-network" in include:
        _drop_empty_regions(topo)
    _add_trust_links(topo)
    _fill_labels(topo)
    if "tags" not in include:
        _drop_tags(topo)
    meta: Dict[str, Any] = {
        "collector": "aws",
        "accounts": sorted(accounts),
        "regions": sorted(set(regions)),
        "region_source": _region_source(scopes),
        "collected_at": _now().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if topo.meta.get("warnings"):
        meta["warnings"] = topo.meta["warnings"]
    topo.meta = meta
    return topo


def run(args) -> Topology:
    boto3 = _require_boto3()
    include = sorted(set(args.include or []) | _wanted(args))
    warnings: List[str] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        accounts = _accounts(boto3, args, pool, warnings)
        account_scopes = list(pool.map(lambda a: _collect_account(a, args.region, include), accounts))
        units = [(a, r) for a, s in zip(accounts, account_scopes) for r in s.regions]
        grants = {a.account_id: s.grants for a, s in zip(accounts, account_scopes)}
        region_scopes = list(pool.map(
            lambda u: _collect_region(u[0], u[1], include, grants.get(u[0].account_id)), units))
    return _assemble(account_scopes + region_scopes, warnings,
                     [a.account_id for a in accounts], [r for _, r in units], include)
