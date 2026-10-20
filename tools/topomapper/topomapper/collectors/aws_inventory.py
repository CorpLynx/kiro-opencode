"""Every resource in an account, for `topomapper account collect resources` (read-only).

AWS has no single "list everything" call, so each region is asked through
whichever inventory services the account has turned on, and their answers
are combined by ARN:

* **AWS Config**, when a recorder is recording: every recorded resource,
  with its relationships ("Is contained in Subnet", "Is associated with
  Role"), which is what places a resource in its VPC or subnet.
* **Resource Explorer**, when the region has an index: every resource it
  indexes, tagged or not, through the default view.
* Only when neither is on, a **fallback**: Cloud Control lists about fifty
  common resource types, and the Resource Groups Tagging API adds anything
  else that carries a tag. It can't promise everything, so it says so.

A resource the network collection already draws (a VPC, a subnet, an
instance) keeps that node; everything else becomes an `aws-resource` node
named by its ARN. It sits in its subnet or VPC when its relationships say
which, and otherwise in a box for its service in its region (or in the
account, for global services such as IAM and CloudFront). `finish()` does
that placement once all regions are in, because a relationship can point at
a resource another region found.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from ..aws_services import service_info
from ..model import (AWS_ACCOUNT, AWS_CGW, AWS_ENDPOINT, AWS_EIGW, AWS_IGW, AWS_INSTANCE,
                     AWS_NAT, AWS_REGION, AWS_RESOURCE, AWS_SERVICE, AWS_SUBNET, AWS_TGW,
                     AWS_VGW, AWS_VPC, LINK_RELATED, Topology, node_id)
from .aws import PARTITION_HOMES, ReadOnlyError, _boto_errors, _error_code, partition_of_region

SOURCES = ("config", "resource-explorer", "cloud-control", "tagging")

# EC2 resources the network collection draws with kinds of their own:
# ARN resource type -> (ID prefix, kind, ID attribute, CloudFormation type).
_NETWORK = {
    "vpc": ("vpc", AWS_VPC, "vpc_id", "AWS::EC2::VPC"),
    "subnet": ("subnet", AWS_SUBNET, "subnet_id", "AWS::EC2::Subnet"),
    "instance": ("instance", AWS_INSTANCE, "instance_id", "AWS::EC2::Instance"),
    "internet-gateway": ("igw", AWS_IGW, "gateway_id", "AWS::EC2::InternetGateway"),
    "egress-only-internet-gateway": ("eigw", AWS_EIGW, "gateway_id",
                                     "AWS::EC2::EgressOnlyInternetGateway"),
    "natgateway": ("nat", AWS_NAT, "nat_gateway_id", "AWS::EC2::NatGateway"),
    "vpc-endpoint": ("endpoint", AWS_ENDPOINT, "endpoint_id", "AWS::EC2::VPCEndpoint"),
    "transit-gateway": ("tgw", AWS_TGW, "tgw_id", "AWS::EC2::TransitGateway"),
    "vpn-gateway": ("vgw", AWS_VGW, "vgw_id", "AWS::EC2::VPNGateway"),
    "customer-gateway": ("cgw", AWS_CGW, "cgw_id", "AWS::EC2::CustomerGateway"),
}
_CFN_NETWORK = {cfn: prefix for prefix, _, _, cfn in _NETWORK.values()}
_REGIONAL_KINDS = (AWS_VPC, AWS_TGW, AWS_VGW, AWS_CGW)  # drawn in the region, not a group

# Config records that are reports about resources, not resources.
_NOT_RESOURCES = frozenset({
    "AWS::Config::ResourceCompliance", "AWS::Config::ConformancePackCompliance",
    "AWS::SSM::ManagedInstanceInventory", "AWS::SSM::AssociationCompliance",
    "AWS::SSM::PatchCompliance", "AWS::SSM::FileData",
})
_GONE = frozenset({"ResourceDeleted", "ResourceDeletedNotRecorded", "ResourceNotRecorded"})

# The fallback's Cloud Control types, with how to build an ARN when the
# listing gives only a name or ID ({p} partition, {r} region, {a} account).
# Global types are listed once, in the partition's home region.
_CLOUD_CONTROL: Dict[str, Tuple[bool, Optional[str]]] = {
    "AWS::ApiGateway::RestApi": (False, "arn:{p}:apigateway:{r}::/restapis/{id}"),
    "AWS::ApiGatewayV2::Api": (False, "arn:{p}:apigateway:{r}::/apis/{id}"),
    "AWS::AppSync::GraphQLApi": (False, None),
    "AWS::Athena::WorkGroup": (False, "arn:{p}:athena:{r}:{a}:workgroup/{id}"),
    "AWS::AutoScaling::AutoScalingGroup": (False, None),
    "AWS::Backup::BackupVault": (False, "arn:{p}:backup:{r}:{a}:backup-vault:{id}"),
    "AWS::CloudFormation::Stack": (False, None),
    "AWS::CloudWatch::Alarm": (False, "arn:{p}:cloudwatch:{r}:{a}:alarm:{id}"),
    "AWS::CodeBuild::Project": (False, "arn:{p}:codebuild:{r}:{a}:project/{id}"),
    "AWS::CodePipeline::Pipeline": (False, "arn:{p}:codepipeline:{r}:{a}:{id}"),
    "AWS::Cognito::UserPool": (False, "arn:{p}:cognito-idp:{r}:{a}:userpool/{id}"),
    "AWS::DynamoDB::Table": (False, "arn:{p}:dynamodb:{r}:{a}:table/{id}"),
    "AWS::EC2::InternetGateway": (False, "arn:{p}:ec2:{r}:{a}:internet-gateway/{id}"),
    "AWS::EC2::Instance": (False, "arn:{p}:ec2:{r}:{a}:instance/{id}"),
    "AWS::EC2::LaunchTemplate": (False, "arn:{p}:ec2:{r}:{a}:launch-template/{id}"),
    "AWS::EC2::NatGateway": (False, "arn:{p}:ec2:{r}:{a}:natgateway/{id}"),
    "AWS::EC2::NetworkInterface": (False, "arn:{p}:ec2:{r}:{a}:network-interface/{id}"),
    "AWS::EC2::SecurityGroup": (False, "arn:{p}:ec2:{r}:{a}:security-group/{id}"),
    "AWS::EC2::Subnet": (False, "arn:{p}:ec2:{r}:{a}:subnet/{id}"),
    "AWS::EC2::TransitGateway": (False, "arn:{p}:ec2:{r}:{a}:transit-gateway/{id}"),
    "AWS::EC2::VPC": (False, "arn:{p}:ec2:{r}:{a}:vpc/{id}"),
    "AWS::EC2::Volume": (False, "arn:{p}:ec2:{r}:{a}:volume/{id}"),
    "AWS::ECR::Repository": (False, "arn:{p}:ecr:{r}:{a}:repository/{id}"),
    "AWS::ECS::Cluster": (False, "arn:{p}:ecs:{r}:{a}:cluster/{id}"),
    "AWS::EFS::FileSystem": (False, "arn:{p}:elasticfilesystem:{r}:{a}:file-system/{id}"),
    "AWS::EKS::Cluster": (False, "arn:{p}:eks:{r}:{a}:cluster/{id}"),
    "AWS::ElasticLoadBalancingV2::LoadBalancer": (False, None),
    "AWS::ElasticLoadBalancingV2::TargetGroup": (False, None),
    "AWS::Events::EventBus": (False, "arn:{p}:events:{r}:{a}:event-bus/{id}"),
    "AWS::Events::Rule": (False, None),
    "AWS::KinesisFirehose::DeliveryStream": (False, "arn:{p}:firehose:{r}:{a}:deliverystream/{id}"),
    "AWS::Kinesis::Stream": (False, "arn:{p}:kinesis:{r}:{a}:stream/{id}"),
    "AWS::KMS::Key": (False, "arn:{p}:kms:{r}:{a}:key/{id}"),
    "AWS::Lambda::Function": (False, "arn:{p}:lambda:{r}:{a}:function:{id}"),
    "AWS::Logs::LogGroup": (False, "arn:{p}:logs:{r}:{a}:log-group:{id}"),
    "AWS::MSK::Cluster": (False, None),
    "AWS::OpenSearchService::Domain": (False, "arn:{p}:es:{r}:{a}:domain/{id}"),
    "AWS::RDS::DBCluster": (False, "arn:{p}:rds:{r}:{a}:cluster:{id}"),
    "AWS::RDS::DBInstance": (False, "arn:{p}:rds:{r}:{a}:db:{id}"),
    "AWS::Redshift::Cluster": (False, "arn:{p}:redshift:{r}:{a}:cluster:{id}"),
    "AWS::SageMaker::Endpoint": (False, None),
    "AWS::SecretsManager::Secret": (False, None),
    "AWS::SNS::Topic": (False, None),
    "AWS::SQS::Queue": (False, None),
    "AWS::SSM::Parameter": (False, "arn:{p}:ssm:{r}:{a}:parameter/{id}"),
    "AWS::StepFunctions::StateMachine": (False, None),
    "AWS::CloudFront::Distribution": (True, "arn:{p}:cloudfront::{a}:distribution/{id}"),
    "AWS::IAM::Role": (True, "arn:{p}:iam::{a}:role/{id}"),
    "AWS::IAM::User": (True, "arn:{p}:iam::{a}:user/{id}"),
    "AWS::Route53::HostedZone": (True, "arn:{p}:route53:::hostedzone/{id}"),
    "AWS::S3::Bucket": (True, "arn:{p}:s3:::{id}"),
}
# Cloud Control errors that only mean "this type can't be listed this way".
_UNLISTABLE = frozenset({"UnsupportedActionException", "TypeNotFoundException",
                         "InvalidRequestException", "ValidationException",
                         "GeneralServiceException", "HandlerInternalFailureException"})


@dataclass
class _Found:
    """One resource as the sources describe it, before it becomes a node."""
    arn: Optional[str]
    key: str                          # node ID
    service: str
    resource_type: str
    resource_id: str
    region: str                       # a region, or "global"
    name: str = ""
    tags: Dict[str, str] = field(default_factory=dict)
    found_by: Set[str] = field(default_factory=set)
    relationships: List[Dict[str, str]] = field(default_factory=list)
    vpc_id: Optional[str] = None
    subnet_ids: List[str] = field(default_factory=list)

    def absorb(self, other: "_Found") -> None:
        """Combine what two sources said about the same resource; Config's
        CloudFormation type name wins over Resource Explorer's."""
        if other.resource_type.startswith("AWS::") or not self.resource_type:
            self.resource_type = other.resource_type
        if other.resource_id and (not self.resource_id or "config" in other.found_by):
            self.resource_id = other.resource_id
        self.name = self.name or other.name
        self.tags = {**other.tags, **self.tags}
        self.found_by |= other.found_by
        self.relationships = self.relationships or other.relationships
        self.vpc_id = self.vpc_id or other.vpc_id
        self.subnet_ids = self.subnet_ids or other.subnet_ids
        if self.region == "global" and other.region != "global":
            self.region = other.region


# -- ARNs --------------------------------------------------------------------------

def parse_arn(arn: str) -> Optional[Tuple[str, str, str, str, str]]:
    """(service, region, account, resource type, resource ID) of an ARN."""
    parts = (arn or "").split(":", 5)
    if len(parts) < 6 or parts[0] != "arn":
        return None
    _, _, service, region, account, resource = parts
    for sep in ("/", ":"):
        if sep in resource:
            rtype, rid = resource.split(sep, 1)
            return service, region, account, rtype, rid
    return service, region, account, "bucket" if service == "s3" else "", resource


def _name_from(service: str, rid: str) -> str:
    rid = rid[:-2] if rid.endswith(":*") else rid
    if service in ("logs", "ssm"):
        return rid
    return rid.rstrip("/").split("/")[-1].split(":")[-1] or rid


def _found(arn: str, source: str, region: str, resource_type: str = "", resource_id: str = "",
           name: str = "", tags: Optional[Dict[str, str]] = None) -> Optional[_Found]:
    parsed = parse_arn(arn)
    if parsed is None:
        return None
    service, arn_region, _, rtype, rid = parsed
    network = _NETWORK.get(rtype) if service == "ec2" else None
    key = (node_id("aws", network[0], rid.split("/")[-1]) if network
           else node_id("aws", "resource", arn))
    return _Found(arn=arn, key=key, service=service,
                  resource_type=resource_type or (f"{service}:{rtype}" if rtype else service),
                  resource_id=resource_id or rid, region=region or arn_region or "global",
                  name=name or (tags or {}).get("Name") or _name_from(service, rid),
                  tags=dict(sorted((tags or {}).items())), found_by={source})


def _tag_dict(tags: Iterable[Dict[str, Any]]) -> Dict[str, str]:
    out = {}
    for tag in tags or []:
        key = tag.get("key", tag.get("Key"))
        if key is not None:
            out[key] = tag.get("value", tag.get("Value", ""))
    return out


def _pages(client: Any, operation: str, key: str, **kwargs: Any) -> List[dict]:
    """Every item of a listing, through its paginator or by NextToken."""
    if client.can_paginate(operation):
        items: List[dict] = []
        for page in client.get_paginator(operation).paginate(**kwargs):
            items.extend(page.get(key, []))
        return items
    items, token = [], None
    while True:
        page = getattr(client, operation)(**kwargs, **({"NextToken": token} if token else {}))
        items.extend(page.get(key, []))
        token = page.get("NextToken")
        if not token:
            return items


# -- sources -------------------------------------------------------------------------

class _Unavailable(Exception):
    """This source isn't turned on here; try the next one."""


def _config(account: Any, region: str) -> List[_Found]:
    config = account.client("config", region, also=("select_resource_config",))
    status = config.describe_configuration_recorder_status().get("ConfigurationRecordersStatus", [])
    if not any(s.get("recording") for s in status):
        raise _Unavailable()
    expression = ("SELECT resourceId, resourceName, resourceType, arn, awsRegion, "
                  "configurationItemStatus, relationships, tags")
    out = []
    for row in _pages(config, "select_resource_config", "Results", Expression=expression):
        item = json.loads(row) if isinstance(row, str) else row
        if item.get("resourceType") in _NOT_RESOURCES or item.get("configurationItemStatus") in _GONE:
            continue
        found = _found(item.get("arn", ""), "config", item.get("awsRegion", ""),
                       item.get("resourceType", ""), item.get("resourceId", ""),
                       item.get("resourceName") or "", _tag_dict(item.get("tags")))
        if found is None:
            continue
        found.relationships = sorted(
            ({"relation": r.get("relationshipName") or r.get("name") or "",
              "type": r.get("resourceType") or "", "id": r.get("resourceId") or ""}
             for r in item.get("relationships") or [] if r.get("resourceId")),
            key=lambda r: (r["relation"], r["type"], r["id"]))
        out.append(found)
    return out


def _resource_explorer(account: Any, region: str, warn: Any) -> List[_Found]:
    explorer = account.client("resource-explorer-2", region)
    try:
        index = explorer.get_index()
    except _boto_errors() as e:
        if _error_code(e) in ("ResourceNotFoundException", "UnauthorizedException"):
            raise _Unavailable() from e
        raise
    if index.get("State") not in ("ACTIVE", "UPDATING"):
        raise _Unavailable()
    view = None
    try:
        view = explorer.get_default_view().get("ViewArn")
    except _boto_errors() as e:
        if _error_code(e) != "ResourceNotFoundException":
            raise
    if not view:
        warn("Resource Explorer has an index here but no default view, so it can't be "
             "searched; set a default view to include it")
        raise _Unavailable()
    query = f"region:{region} region:global"
    if "ListResources" in explorer.meta.service_model.operation_names:
        items = _pages(explorer, "list_resources", "Resources", ViewArn=view,
                       Filters={"FilterString": query})
    else:  # an older botocore: Search stops at 1,000 results
        items = _pages(explorer, "search", "Resources", ViewArn=view, QueryString=query)
        if len(items) >= 1000:
            warn("Resource Explorer search stopped at 1,000 resources; upgrade boto3 for "
                 "the complete list")
    out = []
    for item in items:
        tags: Dict[str, str] = {}
        for prop in item.get("Properties") or []:
            if prop.get("Name") == "tags":
                tags = _tag_dict(prop.get("Data") or [])
        found = _found(item.get("Arn", ""), "resource-explorer", item.get("Region", ""),
                       item.get("ResourceType", ""), tags=tags)
        if found is not None:
            out.append(found)
    return out


def _cloud_control(account: Any, region: str, account_id: str, warn: Any) -> List[_Found]:
    control = account.client("cloudcontrol", region)
    partition = partition_of_region(region)
    home = PARTITION_HOMES.get(partition) == region
    out, denied = [], []
    for type_name, (is_global, template) in sorted(_CLOUD_CONTROL.items()):
        if is_global and not home:
            continue
        try:
            items = _pages(control, "list_resources", "ResourceDescriptions", TypeName=type_name)
        except _boto_errors() as e:
            code = _error_code(e)
            if code not in _UNLISTABLE:
                denied.append(type_name)
            continue
        for item in items:
            found = _from_cloud_control(item, type_name, template, partition, region,
                                        account_id, "global" if is_global else region)
            if found is not None:
                out.append(found)
    if denied:
        warn(f"Cloud Control could not list {len(denied)} resource type(s), e.g. {denied[0]}")
    return out


def _from_cloud_control(item: Dict[str, Any], type_name: str, template: Optional[str],
                        partition: str, region: str, account_id: str,
                        where: str) -> Optional[_Found]:
    identifier = str(item.get("Identifier") or "")
    try:
        props = json.loads(item.get("Properties") or "{}")
    except ValueError:
        props = {}
    if type_name == "AWS::SQS::Queue" and identifier.startswith("https://"):
        identifier = f"arn:{partition}:sqs:{region}:{account_id}:{identifier.rstrip('/').split('/')[-1]}"
    arn = (identifier if identifier.startswith("arn:") else props.get("Arn")
           if str(props.get("Arn", "")).startswith("arn:") else None)
    if arn is None and template:
        arn = template.format(p=partition, r=region, a=account_id, id=identifier.lstrip("/")
                              if type_name == "AWS::SSM::Parameter" else identifier)
    if arn is None:  # no ARN to be had: name it by type and identifier instead
        service = type_name.split("::")[1].lower()
        return _Found(arn=None, key=node_id("aws", "resource", account_id, where, type_name,
                                            identifier),
                      service=service, resource_type=type_name, resource_id=identifier,
                      region=where, name=identifier.split("/")[-1], found_by={"cloud-control"})
    found = _found(arn, "cloud-control", where, type_name, identifier)
    if found is None:
        return None
    vpc_config = props.get("VpcConfig") or {}
    found.vpc_id = props.get("VpcId") or vpc_config.get("VpcId")
    subnets = (props.get("SubnetIds") or vpc_config.get("SubnetIds")
               or props.get("VPCZoneIdentifier") or ([props["SubnetId"]] if props.get("SubnetId")
                                                     else []))
    found.subnet_ids = sorted(s for s in subnets if isinstance(s, str))
    return found


def _tagging(account: Any, region: str) -> List[_Found]:
    tagging = account.client("resourcegroupstaggingapi", region)
    out = []
    for item in _pages(tagging, "get_resources", "ResourceTagMappingList"):
        arn = item.get("ResourceARN", "")
        parsed = parse_arn(arn)
        where = (parsed[1] or (region if parsed[0] == "s3" else "global")) if parsed else region
        found = _found(arn, "tagging", where, tags=_tag_dict(item.get("Tags")))
        if found is not None:
            out.append(found)
    return out


# -- one region ----------------------------------------------------------------------

def collect(scope: Any, account: Any, region: str) -> None:
    """Add every resource the region's inventory services know of to `scope`."""
    found: Dict[str, _Found] = {}
    used: List[str] = []

    def take(source: str, fn: Any) -> bool:
        try:
            items = fn()
        except _Unavailable:
            return False
        except _boto_errors() as e:
            scope.warn(f"cannot read {source} ({_error_code(e)}); its resources are missing")
            return False
        except ReadOnlyError:
            raise
        except Exception as e:  # noqa: BLE001 - one odd answer mustn't cost the other sources
            scope.warn(f"{source}: skipped after unexpected {type(e).__name__}: {e}")
            return False
        used.append(source)
        for item in items:
            if item.key in found:
                found[item.key].absorb(item)
            else:
                found[item.key] = item
        return True

    take("config", lambda: _config(account, region))
    take("resource-explorer", lambda: _resource_explorer(account, region, scope.warn))
    if not used:
        take("cloud-control", lambda: _cloud_control(account, region, scope.account_id, scope.warn))
        take("tagging", lambda: _tagging(account, region))
    scope.topo.add_node(scope.region_node, AWS_REGION, region, scope.account_node,
                        inventory_sources=used or None)
    for item in sorted(found.values(), key=lambda f: f.key):
        _add(scope, item)


def _group(scope: Any, item: _Found) -> str:
    where = item.region if item.region == "global" else scope.region
    gid = node_id("aws", "service", scope.account_id, where, item.service)
    if gid not in scope.topo.nodes:
        parent = scope.account_node if where == "global" else scope.region_node
        scope.topo.add_node(gid, AWS_SERVICE, service_info(item.service)[0], parent,
                            service=item.service, region=where)
    return gid


def _add(scope: Any, item: _Found) -> None:
    attrs: Dict[str, Any] = dict(
        arn=item.arn, resource_type=item.resource_type, resource_id=item.resource_id,
        service=item.service, region=item.region, found_by=sorted(item.found_by),
        relationships=item.relationships or None, vpc_id=item.vpc_id,
        subnet_ids=item.subnet_ids or None, tags=item.tags or None)
    if not item.key.startswith("aws:resource:"):
        if item.key in scope.topo.nodes:
            return  # the network collection drew it already
        prefix = item.key.split(":")[1]
        _, kind, id_attr, _ = next(v for v in _NETWORK.values() if v[0] == prefix)
        parent = scope.region_node if kind in _REGIONAL_KINDS else _group(scope, item)
        label = f"{item.name} ({item.resource_id})" if item.name != item.resource_id else item.name
        attrs[id_attr] = item.resource_id
        scope.topo.add_node(item.key, kind, label, parent, **attrs)
        return
    scope.topo.add_node(item.key, AWS_RESOURCE, item.name, _group(scope, item), name=item.name,
                        account_id=scope.account_id, **attrs)


# -- the whole collection ---------------------------------------------------------------

def finish(topo: Topology, links: bool = False) -> None:
    """Place resources by their relationships, optionally draw the
    relationships, and tidy the service boxes. Runs once every region is in."""
    _dedupe(topo)
    index = _index(topo)
    placed: Set[Tuple[str, str]] = set()
    for node in sorted(_inventory(topo), key=lambda n: n.id):
        parent, used = _placement(topo, node, index)
        if parent and parent != node.id and not _is_inside(topo, parent, node.id):
            node.parent = parent
            placed |= {(node.id, u) for u in used}
    if links:
        for node in sorted(_inventory(topo), key=lambda n: n.id):
            for rel in node.attrs.get("relationships") or []:
                target = index.get((rel["type"], rel["id"]))
                if (not target or target == node.id or (node.id, target) in placed
                        or rel["relation"].startswith("Contains")):
                    continue
                topo.add_link(LINK_RELATED, node.id, target, directed=True, key=rel["relation"],
                              relation=rel["relation"])
    _tidy_groups(topo)
    _fallback_warning(topo)


def _inventory(topo: Topology) -> List[Any]:
    return [n for n in topo.nodes.values() if n.attrs.get("found_by")]


def _dedupe(topo: Topology) -> None:
    """A resource another option drew in its own way (an EKS cluster, an IAM
    role) keeps that node; the inventory's copy goes."""
    own = {n.attrs["arn"]: n.id for n in topo.nodes.values()
           if n.kind != AWS_RESOURCE and isinstance(n.attrs.get("arn"), str)}
    for node in [n for n in topo.by_kind(AWS_RESOURCE) if n.attrs.get("arn") in own]:
        keep = own[node.attrs["arn"]]
        for child in topo.children(node.id):
            child.parent = keep
        topo.nodes[keep].attrs.setdefault("found_by", node.attrs.get("found_by"))
        del topo.nodes[node.id]


def _index(topo: Topology) -> Dict[Tuple[str, str], str]:
    """(CloudFormation type, resource ID) -> node, which is how Config
    relationships name their other end."""
    index: Dict[Tuple[str, str], str] = {}
    for node in topo.nodes.values():
        rtype, rid = node.attrs.get("resource_type"), node.attrs.get("resource_id")
        if rtype and rid:
            index[(rtype, rid)] = node.id
    for cfn, prefix in _CFN_NETWORK.items():
        for node in topo.nodes.values():
            if node.id.startswith(f"aws:{prefix}:"):
                index.setdefault((cfn, node.id.split(":", 2)[2]), node.id)
    return index


def _vpc_of(topo: Topology, nid: str) -> Optional[str]:
    seen = set()
    while nid and nid not in seen:
        seen.add(nid)
        node = topo.nodes.get(nid)
        if node is None:
            return None
        if node.kind == AWS_VPC:
            return nid
        nid = node.parent or ""
    return None


def _is_inside(topo: Topology, nid: str, ancestor: str) -> bool:
    """Whether `nid` sits somewhere inside `ancestor` (so making it the
    parent would loop)."""
    seen = set()
    while nid and nid not in seen:
        if nid == ancestor:
            return True
        seen.add(nid)
        nid = (topo.nodes[nid].parent or "") if nid in topo.nodes else ""
    return False


def _placement(topo: Topology, node: Any, index: Dict[Tuple[str, str], str]
               ) -> Tuple[Optional[str], List[str]]:
    """Where a resource belongs: its one subnet, or the VPC when it spans
    several subnets or names only a VPC. Resources already somewhere more
    specific than a region or service box stay put."""
    current = topo.nodes.get(node.parent or "")
    if current is not None and current.kind not in (AWS_SERVICE, AWS_REGION, AWS_ACCOUNT):
        return None, []
    subnets: List[str] = []
    vpcs: List[str] = []
    for rel in node.attrs.get("relationships") or []:
        target = index.get((rel["type"], rel["id"]))
        if target and topo.nodes[target].kind == AWS_SUBNET:
            subnets.append(target)
        elif target and topo.nodes[target].kind == AWS_VPC:
            vpcs.append(target)
    subnets += [node_id("aws", "subnet", s) for s in node.attrs.get("subnet_ids") or []
                if node_id("aws", "subnet", s) in topo.nodes]
    if node.attrs.get("vpc_id") and node_id("aws", "vpc", node.attrs["vpc_id"]) in topo.nodes:
        vpcs.append(node_id("aws", "vpc", node.attrs["vpc_id"]))
    subnets, vpcs = sorted(set(subnets)), sorted(set(vpcs))
    if node.kind == AWS_SUBNET:
        subnets = []  # a subnet goes in its VPC, never in another subnet
    if len(subnets) == 1:
        return subnets[0], subnets
    if subnets:
        homes = {_vpc_of(topo, s) for s in subnets}
        if len(homes) == 1 and None not in homes:
            return homes.pop(), subnets + vpcs
    if len(vpcs) == 1:
        return vpcs[0], vpcs
    return None, []


def _tidy_groups(topo: Topology) -> None:
    """Service boxes left empty by placement go; the rest say how many
    resources they hold."""
    for group in topo.by_kind(AWS_SERVICE):
        children = topo.children(group.id)
        if not children:
            del topo.nodes[group.id]
        else:
            group.attrs["resources"] = len(children)


def _fallback_warning(topo: Topology) -> None:
    """One warning per account whose regions had neither Config nor Resource
    Explorer, since the fallback can miss resource types."""
    by_account: Dict[str, List[str]] = {}
    for region in topo.by_kind(AWS_REGION):
        sources = region.attrs.get("inventory_sources")
        if sources is not None and not {"config", "resource-explorer"} & set(sources):
            account = topo.nodes.get(region.parent or "")
            if account is not None and account.kind == AWS_ACCOUNT:
                by_account.setdefault(account.attrs.get("account_id") or account.id, []).append(
                    region.label)
    for account_id, regions in sorted(by_account.items()):
        topo.warn(f"aws {account_id}: neither AWS Config nor Resource Explorer is on in "
                  f"{len(regions)} region(s) ({', '.join(sorted(regions)[:5])}"
                  f"{', ...' if len(regions) > 5 else ''}); listed common resource types and "
                  "tagged resources instead, so some may be missing. Turn on Resource Explorer "
                  "for a complete list")
