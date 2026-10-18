"""EKS clusters in an account and region (read-only).

`collect aws --eks` adds one `eks-cluster` node per cluster, inside the VPC
it runs in (or in the region when that VPC wasn't collected). The cluster's
facts sit in its attributes: version and status, who can reach its API
endpoint, its logging, the IAM role it uses and its OIDC issuer (which is
what ties IRSA roles to it), plus its node groups, Fargate profiles and
add-ons. Cluster contents (pods, namespaces) need the Kubernetes API, not
AWS's, and are out of scope.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..model import AWS_EKS, node_id
from .aws import _boto_errors, _ensure_vpc, _error_code, _paginate, _tags
from .aws_ai import in_region


def _date(value: Any) -> Optional[str]:
    return value.strftime("%Y-%m-%d") if hasattr(value, "strftime") else None


def _nodegroups(eks: Any, name: str) -> List[Dict[str, Any]]:
    out = []
    for group in sorted(_paginate(eks, "list_nodegroups", "nodegroups", clusterName=name)):
        d = eks.describe_nodegroup(clusterName=name, nodegroupName=group)["nodegroup"]
        scaling = d.get("scalingConfig") or {}
        out.append({
            "name": d.get("nodegroupName", group), "status": d.get("status"),
            "instance_types": d.get("instanceTypes") or None,
            "capacity_type": d.get("capacityType"), "ami_type": d.get("amiType"),
            "version": d.get("version"), "desired": scaling.get("desiredSize"),
            "min": scaling.get("minSize"), "max": scaling.get("maxSize"),
        })
    return out


def _fargate(eks: Any, name: str) -> List[Dict[str, Any]]:
    out = []
    for profile in sorted(_paginate(eks, "list_fargate_profiles", "fargateProfileNames",
                                    clusterName=name)):
        d = eks.describe_fargate_profile(clusterName=name,
                                         fargateProfileName=profile)["fargateProfile"]
        out.append({"name": d.get("fargateProfileName", profile), "status": d.get("status"),
                    "namespaces": sorted(s.get("namespace", "") for s in d.get("selectors") or [])
                    or None})
    return out


def _cluster(scope: Any, eks: Any, name: str) -> None:
    c = eks.describe_cluster(name=name)["cluster"]
    vpc = c.get("resourcesVpcConfig") or {}
    logging = sorted(t for entry in (c.get("logging") or {}).get("clusterLogging", [])
                     if entry.get("enabled") for t in entry.get("types", []))
    tags = _tags({"tags": [{"key": k, "value": v} for k, v in (c.get("tags") or {}).items()]})
    parent = _ensure_vpc(scope, vpc["vpcId"]) if vpc.get("vpcId") else scope.region_node
    attrs: Dict[str, Any] = dict(
        cluster_name=c.get("name", name), arn=c.get("arn"), version=c.get("version"),
        status=c.get("status"), platform_version=c.get("platformVersion"),
        created=_date(c.get("createdAt")), role_arn=c.get("roleArn"),
        endpoint_public_access=vpc.get("endpointPublicAccess"),
        endpoint_private_access=vpc.get("endpointPrivateAccess"),
        public_access_cidrs=vpc.get("publicAccessCidrs") if vpc.get("endpointPublicAccess")
        else None,
        subnet_ids=sorted(vpc.get("subnetIds") or []) or None,
        security_group_ids=sorted(([vpc["clusterSecurityGroupId"]]
                                   if vpc.get("clusterSecurityGroupId") else [])
                                  + list(vpc.get("securityGroupIds") or [])) or None,
        oidc_issuer=((c.get("identity") or {}).get("oidc") or {}).get("issuer"),
        logging_enabled=logging or None, secrets_encrypted=True if c.get("encryptionConfig")
        else None, vpc_id=vpc.get("vpcId"), account_id=scope.account_id, region=scope.region)
    attrs["nodegroups"] = scope.attempt(f"read node groups of EKS cluster {name}", _nodegroups, eks,
                                        name) or None
    attrs["fargate_profiles"] = scope.attempt(f"read Fargate profiles of EKS cluster {name}", _fargate,
                                              eks, name) or None
    addons = scope.attempt(f"list add-ons of EKS cluster {name}", _paginate, eks, "list_addons", "addons",
                           clusterName=name)
    attrs["addons"] = sorted(addons) or None if addons is not None else None
    scope.topo.add_node(node_id("aws", AWS_EKS, scope.account_id, scope.region, name), AWS_EKS,
                        name, parent, tags=tags or None, **attrs)


def collect(scope: Any, eks: Any) -> None:
    """Add the region's EKS clusters to `scope`."""
    if not in_region("eks", scope.region):
        return
    names = scope.attempt("list EKS clusters", _paginate, eks, "list_clusters", "clusters",
                          skip="EKS in this region") or []
    for name in sorted(names):
        try:
            _cluster(scope, eks, name)
        except _boto_errors() as e:
            scope.warn(f"cannot describe EKS cluster {name} ({_error_code(e)}); it is left out")
