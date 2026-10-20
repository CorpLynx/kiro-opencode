"""Custom IAM roles and users in an account (read-only).

`topomapper account collect iam` adds one `iam-role` node per role and one `iam-user`
node per user. They sit in an `iam` box, one per account, which the
renderers pin to the account's top-right corner so IAM is always in the
same place. Roles that AWS owns (service-linked
roles and the roles IAM Identity Center creates) are left out unless
`--all-roles` is given, since every account has dozens and they say nothing
about how it is run.

What is recorded is what answers "who can get in and with what": for a role
its trust (accounts, services and federated providers allowed to assume
it, and whether it trusts anyone at all) and its policy names; for a user
its groups, policy names, whether MFA is on and the age of its access keys.
No policy documents, credentials or key IDs are read. A role that trusts a
principal in another collected account also gets an `iam-trust` link, drawn
from that account to the role.

Everything comes from one paginated `GetAccountAuthorizationDetails` call,
falling back to `ListRoles` / `ListUsers` (less detail) if it is denied.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote

from ..model import AWS_IAM, AWS_IAM_ROLE, AWS_IAM_USER, LINK_TRUST, Topology, node_id
from .aws import _boto_errors, _error_code, _paginate

AWS_OWNED_PATHS = ("/aws-service-role/", "/aws-reserved/")
AWS_OWNED_PREFIXES = ("AWSServiceRoleFor", "AWSReservedSSO_")
_PRINCIPAL_ARN = re.compile(r"^arn:[\w-]+:iam::(\d{12}):(root|role|user)(?:/(.*))?$")
_ACCOUNT_ID = re.compile(r"^\d{12}$")


def _date(value: Any) -> Optional[str]:
    return value.strftime("%Y-%m-%d") if isinstance(value, datetime) else None


def aws_owned(role: Dict[str, Any]) -> bool:
    """Service-linked roles and the roles IAM Identity Center manages."""
    return (str(role.get("Path") or "/").startswith(AWS_OWNED_PATHS)
            or str(role.get("RoleName") or "").startswith(AWS_OWNED_PREFIXES))


def _document(value: Any) -> Dict[str, Any]:
    """A policy document as a dict; boto3 decodes them, but a raw URL-encoded
    string is accepted too."""
    if isinstance(value, str):
        try:
            value = json.loads(unquote(value))
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value]
    return [v for v in value or [] if isinstance(v, str)]


def trust_summary(document: Any, own_account: str = "") -> Dict[str, Any]:
    """Who the trust policy lets assume the role.

    Only Allow statements count. `trusts_everyone` is true for a principal
    of `*`, which with no condition means any AWS principal at all.
    `trusted_accounts` lists other accounts only; trusting the role's own
    account is `trusts_own_account`, and named roles and users in either
    account stay in `trusted_principals` so they can be linked.
    """
    statements = _document(document).get("Statement") or []
    if isinstance(statements, dict):
        statements = [statements]
    accounts, services, federated, principals = set(), set(), set(), []
    everyone = False
    for statement in statements:
        if not isinstance(statement, dict) or statement.get("Effect") != "Allow":
            continue
        principal = statement.get("Principal")
        if principal == "*":
            everyone = True
            continue
        if not isinstance(principal, dict):
            continue
        for key, values in principal.items():
            for value in _as_list(values):
                if key == "Service":
                    services.add(value)
                elif key == "Federated":
                    federated.add(value)
                elif key == "AWS":
                    if value == "*":
                        everyone = True
                    elif _ACCOUNT_ID.match(value):
                        accounts.add(value)
                        principals.append({"account": value, "type": "root"})
                    else:
                        m = _PRINCIPAL_ARN.match(value)
                        if m:
                            accounts.add(m.group(1))
                            principals.append({"account": m.group(1), "type": m.group(2),
                                               "name": (m.group(3) or "").rsplit("/", 1)[-1]
                                               or None})
    others = sorted(accounts - {own_account})
    kept = [p for p in principals if p["account"] != own_account or p["type"] != "root"]
    return {
        "trusted_accounts": others or None,
        "trusts_own_account": True if own_account and own_account in accounts else None,
        "trusted_principals": kept or None,
        "trusted_services": sorted(services) or None,
        "trusted_federated": sorted(federated) or None,
        "trusts_everyone": True if everyone else None,
    }


def _policy_names(detail: Dict[str, Any]) -> Dict[str, Any]:
    attached = sorted(p["PolicyName"] for p in detail.get("AttachedManagedPolicies") or [])
    inline = sorted(p["PolicyName"] for p in (detail.get("RolePolicyList")
                                              or detail.get("UserPolicyList") or []))
    boundary = (detail.get("PermissionsBoundary") or {}).get("PermissionsBoundaryArn")
    return {
        "attached_policies": attached or None,
        "inline_policies": inline or None,
        "permissions_boundary": boundary.rsplit("/", 1)[-1] if boundary else None,
        "admin_access": True if "AdministratorAccess" in attached else None,
    }


def _authorization_details(iam: Any) -> Tuple[List[dict], List[dict]]:
    roles: List[dict] = []
    users: List[dict] = []
    for page in iam.get_paginator("get_account_authorization_details").paginate(
            Filter=["User", "Role"]):
        roles.extend(page.get("RoleDetailList", []))
        users.extend(page.get("UserDetailList", []))
    return roles, users


def _listing(iam: Any) -> Tuple[List[dict], List[dict]]:
    return (_paginate(iam, "list_roles", "Roles"), _paginate(iam, "list_users", "Users"))


def iam_box(scope: Any) -> str:
    """The account's `iam` box, created on first use; its ID is what roles
    and users take as their parent."""
    box = node_id("aws", AWS_IAM, scope.account_id)
    scope.topo.add_node(box, AWS_IAM, "IAM", scope.account_node, account_id=scope.account_id)
    return box


def _role_node(scope: Any, role: Dict[str, Any]) -> None:
    name = role["RoleName"]
    last_used = (role.get("RoleLastUsed") or {}).get("LastUsedDate")
    attrs: Dict[str, Any] = {
        "role_name": name, "path": role.get("Path") if role.get("Path") != "/" else None,
        "arn": role.get("Arn"), "account_id": scope.account_id,
        "created": _date(role.get("CreateDate")), "last_used": _date(last_used),
        "aws_owned": True if aws_owned(role) else None,
    }
    attrs.update(trust_summary(role.get("AssumeRolePolicyDocument"), scope.account_id))
    attrs.update(_policy_names(role))
    tags = {t["Key"]: t.get("Value", "") for t in role.get("Tags") or [] if "Key" in t}
    scope.topo.add_node(node_id("aws", AWS_IAM_ROLE, scope.account_id, name), AWS_IAM_ROLE, name,
                        iam_box(scope), tags=dict(sorted(tags.items())) or None, **attrs)


def _user_security(scope: Any, iam: Any, names: List[str]) -> Dict[str, Dict[str, Any]]:
    """MFA and access-key facts per user. If the first call is refused the
    rest would be too, so that is one warning rather than one per user."""
    out: Dict[str, Dict[str, Any]] = {}
    for name in names:
        try:
            mfa = iam.list_mfa_devices(UserName=name).get("MFADevices", [])
            keys = iam.list_access_keys(UserName=name).get("AccessKeyMetadata", [])
        except _boto_errors() as e:
            scope.warn(f"cannot read MFA and access keys of IAM users ({_error_code(e)}); "
                       "those fields are left out")
            return out
        active = [k for k in keys if k.get("Status") == "Active"]
        out[name] = {
            "mfa_enabled": bool(mfa),
            "access_keys": [{"status": k.get("Status"), "created": _date(k.get("CreateDate"))}
                            for k in keys] or None,
            "active_access_keys": len(active),
        }
    return out


def _user_nodes(scope: Any, iam: Any, users: List[Dict[str, Any]]) -> None:
    security = _user_security(scope, iam, sorted(u["UserName"] for u in users))
    for user in users:
        name = user["UserName"]
        attrs: Dict[str, Any] = {
            "user_name": name, "path": user.get("Path") if user.get("Path") != "/" else None,
            "arn": user.get("Arn"), "account_id": scope.account_id,
            "created": _date(user.get("CreateDate")),
            "password_last_used": _date(user.get("PasswordLastUsed")),
            "groups": sorted(user.get("GroupList") or []) or None,
        }
        attrs.update(_policy_names(user))
        attrs.update(security.get(name, {}))
        tags = {t["Key"]: t.get("Value", "") for t in user.get("Tags") or [] if "Key" in t}
        scope.topo.add_node(node_id("aws", AWS_IAM_USER, scope.account_id, name), AWS_IAM_USER,
                            name, iam_box(scope), tags=dict(sorted(tags.items())) or None,
                            **attrs)


def collect(scope: Any, iam: Any, include_aws_owned: bool = False) -> None:
    """Add the account's IAM roles and users to `scope`."""
    try:
        roles, users = _authorization_details(iam)
    except _boto_errors() as e:
        scope.warn(f"cannot get IAM account details ({_error_code(e)}); falling back to "
                   "listing roles and users, which has less detail")
        roles, users = scope.attempt("list IAM roles and users", _listing, iam,
                                     skip="IAM") or ([], [])
    for role in sorted(roles, key=lambda r: r["RoleName"]):
        if include_aws_owned or not aws_owned(role):
            _role_node(scope, role)
    _user_nodes(scope, iam, sorted(users, key=lambda u: u["UserName"]))
    _summarize(scope)


def _summarize(scope: Any) -> None:
    """Put "N roles, M users" on the account's IAM box."""
    box = node_id("aws", AWS_IAM, scope.account_id)
    if box not in scope.topo.nodes:
        return
    kinds = [n.kind for n in scope.topo.children(box)]
    parts = [f"{kinds.count(k)} {word}{'' if kinds.count(k) == 1 else 's'}"
             for k, word in ((AWS_IAM_ROLE, "role"), (AWS_IAM_USER, "user")) if kinds.count(k)]
    scope.topo.nodes[box].attrs["summary"] = ", ".join(parts) or None


def add_trust_links(topo: Topology) -> None:
    """Link each role to the collected accounts (and, when collected, the
    roles and users) that its trust policy allows to assume it. Within one
    account only role and user principals are linked: a link from a role's
    own account would just point at its container."""
    for role in topo.by_kind(AWS_IAM_ROLE):
        own = role.attrs.get("account_id") or role.id.split(":")[2]
        for principal in role.attrs.get("trusted_principals") or []:
            account = node_id("aws", "account", principal["account"])
            if account not in topo.nodes:
                continue
            source = account
            if principal["type"] in ("role", "user") and principal.get("name"):
                named = node_id("aws", f"iam-{principal['type']}", principal["account"],
                                principal["name"])
                if named in topo.nodes:
                    source = named
            if source == account and principal["account"] == own:
                continue
            topo.add_link(LINK_TRUST, source, role.id, directed=True, principal=principal["type"])
