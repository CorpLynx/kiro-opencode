"""Which Bedrock models an account actually uses or grants, per region (read-only).

`topomapper aws --ai` adds, for each account and region where Bedrock exists,
a `bedrock` group holding one `bedrock-model` node per model that has
evidence behind it, plus the account's custom and imported models. A
foundation model is drawn when either:

* **it is in use:** CloudWatch has Bedrock metrics for it in the last two
  weeks (`ListMetrics` on `AWS/Bedrock`, dimension `ModelId`; inference
  profiles such as `us.anthropic...` count for the model behind them); or
* **it is granted:** an IAM policy in that account allows
  `bedrock:InvokeModel` (or Converse) on it by name, or by a pattern such
  as `anthropic.claude-*`.

Why not "every model the API lists as available": Bedrock now turns most
models on for everyone, so availability says the same about every account.
Which models are actually offered is only visible in the count kept on the
group. A policy that grants every model (a `*` resource, or
AmazonBedrockFullAccess) is recorded as `granted_all_to`, not drawn as
hundreds of nodes.

A drawn model also gets `invocable`: false when the availability API says
the account is not authorized, not entitled, or hasn't an agreement in
place (the common case for Anthropic models until their use-case form is
filled in). A grant to a model nobody can call is worth seeing.

Grants come from `GetAccountAuthorizationDetails`, which IAM collection
uses too. Deny statements and permission boundaries aren't evaluated, so a
grant is "an Allow exists", not "the call would succeed".
"""

from __future__ import annotations

import fnmatch
import re
from functools import lru_cache
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

from ..model import AWS_BEDROCK, AWS_BEDROCK_MODEL, AWS_REGION, node_id
from .aws import _boto_errors, _error_code, _paginate, partition_of_region

# Optional listings that some regions don't offer; their failure isn't news.
_NOT_OFFERED = frozenset({"UnknownOperationException", "ValidationException",
                          "AccessDeniedException", "AccessDenied", "UnsupportedOperation"})
_GEO_PREFIXES = frozenset({"us", "eu", "apac", "global", "us-gov", "jp", "au", "ca", "in", "ap"})
_INVOKE = ("bedrock:invokemodel", "bedrock:invokemodelwithresponsestream", "bedrock:converse",
           "bedrock:conversestream")
_FULL_ACCESS_POLICIES = frozenset({"AmazonBedrockFullAccess"})
_MODEL_RESOURCE = re.compile(r"^arn:[\w-]+:bedrock:([\w*?-]*):[\w*?]*:"
                             r"(foundation-model|inference-profile)/(.+)$")
_ALL = "*"


@lru_cache(maxsize=None)
def service_regions(service: str, partition: str = "aws") -> FrozenSet[str]:
    """Regions botocore knows `service` in, within one partition; empty when
    it can't say, which callers treat as "try it"."""
    try:
        import boto3
        return frozenset(boto3.session.Session().get_available_regions(
            service, partition_name=partition))
    except Exception:  # noqa: BLE001 - an old botocore, or no data for the service
        return frozenset()


def in_region(service: str, region: str) -> bool:
    """Whether `service` exists in `region`. GovCloud and China regions are
    looked up in their own partition, not the commercial one."""
    known = service_regions(service, partition_of_region(region))
    return not known or region in known


def is_enabled(availability: Dict[str, Any]) -> bool:
    """Whether a `GetFoundationModelAvailability` answer means "can invoke"."""
    agreement = (availability.get("agreementAvailability") or {}).get("status")
    return (availability.get("authorizationStatus") == "AUTHORIZED"
            and availability.get("entitlementAvailability") == "AVAILABLE"
            and availability.get("regionAvailability") == "AVAILABLE"
            and agreement in (None, "AVAILABLE"))


def base_model_id(text: str) -> str:
    """The model behind a model or inference-profile ID: `us.anthropic.x`
    and `anthropic.x` are the same model."""
    head, _, rest = text.partition(".")
    return rest if head in _GEO_PREFIXES and "." in rest else text


# -- who is granted what -----------------------------------------------------------

def _statements(document: Any) -> List[dict]:
    if isinstance(document, str):
        return []
    statements = (document or {}).get("Statement") or []
    return [statements] if isinstance(statements, dict) else [s for s in statements
                                                              if isinstance(s, dict)]


def _list(value: Any) -> List[str]:
    return [value] if isinstance(value, str) else [v for v in value or [] if isinstance(v, str)]


def grants_in(document: Any) -> Tuple[Set[Tuple[str, str]], Set[str]]:
    """({(model pattern, region)}, {regions where every model is granted}).

    Only Allow statements that name an invoke action count; a region of `*`
    means any region.
    """
    models: Set[Tuple[str, str]] = set()
    everywhere: Set[str] = set()
    for statement in _statements(document):
        if statement.get("Effect") != "Allow":
            continue
        actions = [a.lower() for a in _list(statement.get("Action"))]
        if not any(fnmatch.fnmatchcase(invoke, pattern) for pattern in actions
                   for invoke in _INVOKE):
            continue
        for resource in _list(statement.get("Resource")):
            if resource == _ALL:
                everywhere.add(_ALL)
                continue
            m = _MODEL_RESOURCE.match(resource)
            if not m:
                continue
            region, _, model = m.groups()
            region = region or _ALL
            model = base_model_id(model)
            if model == _ALL:
                everywhere.add(region)
            else:
                models.add((model, region))
    return models, everywhere


class Grants:
    """Models granted by IAM policies in one account, and to whom."""

    def __init__(self) -> None:
        self.models: Dict[Tuple[str, str], Set[str]] = {}
        self.everywhere: Dict[str, Set[str]] = {}

    def add(self, principal: str, document: Any) -> None:
        models, everywhere = grants_in(document)
        for key in models:
            self.models.setdefault(key, set()).add(principal)
        for region in everywhere:
            self.everywhere.setdefault(region, set()).add(principal)

    def add_full_access(self, principal: str) -> None:
        self.everywhere.setdefault(_ALL, set()).add(principal)

    def for_region(self, region: str, offered: Iterable[str]
                   ) -> Tuple[Dict[str, Set[str]], Set[str]]:
        """({model ID: principals} that are named, {principals granted all})
        for one region; patterns are expanded against the models `offered`."""
        offered = sorted(set(offered))
        named: Dict[str, Set[str]] = {}
        for (pattern, where), who in sorted(self.models.items()):
            if where not in (_ALL, region):
                continue
            for model in (m for m in offered if fnmatch.fnmatchcase(m, pattern)):
                named.setdefault(model, set()).update(who)
        everyone: Set[str] = set()
        for where in (_ALL, region):
            everyone |= self.everywhere.get(where, set())
        return named, everyone


def collect_grants(scope: Any, iam: Any) -> Grants:
    """Read the account's IAM policies for Bedrock grants."""
    grants = Grants()
    try:
        pages = iam.get_paginator("get_account_authorization_details").paginate(
            Filter=["User", "Role", "Group", "LocalManagedPolicy"])
        managed: Dict[str, Any] = {}
        details: List[Tuple[str, dict, str]] = []
        for page in pages:
            for policy in page.get("Policies", []):
                version = next((v for v in policy.get("PolicyVersionList", [])
                                if v.get("IsDefaultVersion")), None)
                managed[policy["Arn"]] = (version or {}).get("Document")
            for key, name_key, inline_key in (("RoleDetailList", "RoleName", "RolePolicyList"),
                                              ("UserDetailList", "UserName", "UserPolicyList"),
                                              ("GroupDetailList", "GroupName", "GroupPolicyList")):
                details.extend((d[name_key], d, inline_key) for d in page.get(key, []))
    except _boto_errors() as e:
        scope.warn(f"cannot read IAM policies for Bedrock grants ({_error_code(e)}); "
                   "only models in use are shown")
        return grants
    for name, detail, inline_key in details:
        for inline in detail.get(inline_key) or []:
            grants.add(name, inline.get("PolicyDocument"))
        for attached in detail.get("AttachedManagedPolicies") or []:
            if attached.get("PolicyName") in _FULL_ACCESS_POLICIES:
                grants.add_full_access(name)
            elif attached.get("PolicyArn") in managed:
                grants.add(name, managed[attached["PolicyArn"]])
    return grants


# -- what is in use -----------------------------------------------------------------

def models_in_use(scope: Any, cloudwatch: Any) -> Set[str]:
    """Model IDs with Bedrock metrics in the last two weeks, in this region."""
    found: Set[str] = set()
    metrics = scope.attempt("list Bedrock usage metrics", _paginate, cloudwatch, "list_metrics",
                            "Metrics", Namespace="AWS/Bedrock")
    for metric in metrics or []:
        for dimension in metric.get("Dimensions", []):
            if dimension.get("Name") == "ModelId" and dimension.get("Value"):
                found.add(base_model_id(dimension["Value"]))
    return found


# -- building the nodes -------------------------------------------------------------

def _quiet(client: Any, operation: str, key: str) -> List[dict]:
    """An optional listing; regions that don't offer it just have none."""
    try:
        return _paginate(client, operation, key)
    except _boto_errors() as e:
        if _error_code(e) in _NOT_OFFERED:
            return []
        raise


def _invocable(client: Any, model_id: str) -> Optional[bool]:
    try:
        return is_enabled(client.get_foundation_model_availability(modelId=model_id))
    except _boto_errors():
        return None


def _model_node(scope: Any, group: str, model_id: str, label: str, **attrs: Any) -> None:
    scope.topo.add_node(node_id("aws", AWS_BEDROCK_MODEL, scope.account_id, scope.region, model_id),
                        AWS_BEDROCK_MODEL, label, group, model_id=model_id, **attrs)


def collect(scope: Any, account: Any, region: str, grants: Optional[Grants] = None) -> None:
    """Add the Bedrock group and models for one (account, region) to `scope`."""
    if not in_region("bedrock", region):
        return
    client = account.client("bedrock", region)
    summaries = scope.attempt("list Bedrock foundation models", _paginate, client,
                              "list_foundation_models", "modelSummaries",
                              skip="Bedrock in this region") or []
    by_id = {m["modelId"]: m for m in summaries if m.get("modelId")}
    used = models_in_use(scope, account.client("cloudwatch", region)) if summaries else set()
    named, everyone = (grants or Grants()).for_region(region, by_id)
    # A grant for a model this region doesn't offer can't be used here.
    shown = sorted(used | (set(named) & set(by_id)))

    extras: Dict[str, List[dict]] = {}
    for key, operation, field in (("custom", "list_custom_models", "modelSummaries"),
                                  ("imported", "list_imported_models", "modelSummaries"),
                                  ("provisioned", "list_provisioned_model_throughputs",
                                   "provisionedModelSummaries")):
        extras[key] = scope.attempt(f"list Bedrock {key} models", _quiet, client, operation,
                                    field) or []

    scope.topo.add_node(scope.region_node, AWS_REGION, scope.region, scope.account_node,
                        region=scope.region, bedrock_models_shown=len(shown),
                        bedrock_models_offered=len(summaries))
    if not (shown or everyone or extras["custom"] or extras["imported"]):
        return
    group = node_id("aws", AWS_BEDROCK, scope.account_id, scope.region)
    scope.topo.add_node(
        group, AWS_BEDROCK, "Amazon Bedrock", scope.region_node, region=scope.region,
        models_shown=len(shown), models_offered=len(summaries),
        models_in_use=len(used) or None,
        granted_all_to=sorted(everyone) or None,
        provisioned_throughput=[
            {"model_units": p.get("modelUnits"), "status": p.get("status"),
             "name": p.get("provisionedModelName")} for p in extras["provisioned"]] or None)
    for model_id in shown:
        m = by_id.get(model_id, {})
        evidence = [e for e, hit in (("in use", model_id in used), ("granted", model_id in named))
                    if hit]
        _model_node(scope, group, model_id, m.get("modelName") or model_id,
                    model_name=m.get("modelName"), provider=m.get("providerName"),
                    input_modalities=m.get("inputModalities") or None,
                    output_modalities=m.get("outputModalities") or None,
                    inference_types=m.get("inferenceTypesSupported") or None,
                    streaming=m.get("responseStreamingSupported"),
                    lifecycle=(m.get("modelLifecycle") or {}).get("status"),
                    source="foundation", evidence=evidence,
                    granted_to=sorted(named.get(model_id, ())) or None,
                    invocable=_invocable(client, model_id) if m else None)
    for kind in ("custom", "imported"):
        for m in extras[kind]:
            name = m.get("modelName") or m.get("modelArn", "").rsplit("/", 1)[-1]
            _model_node(scope, group, f"{kind}/{name}", name, model_name=name, source=kind,
                        base_model=m.get("baseModelName"), arn=m.get("modelArn"))
