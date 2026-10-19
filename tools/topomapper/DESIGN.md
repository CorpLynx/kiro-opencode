# topomapper design

`topomapper` gathers AWS topology automatically, merges the pieces, and draws them.

```
aws      boto3, read-only API calls         ─┐
aws-org  the AWS Organization: OUs, accounts ─┴─▶ topomap/1 JSON ─▶ merge ─▶ filter ─▶ render
                                                                └─▶ diff (drift between snapshots)
```

Every stage reads and writes the same JSON graph (`topomapper/model.py`), so stages compose with files or pipes, and snapshots can be committed and diffed.

## Ground rules

- **Python 3.9+.** Use `from __future__ import annotations` and `typing.Optional`/`List`/`Dict`. Don't use `X | None` outside annotations.
- **Optional dependencies:** the core (model, merge/diff/filter/anonymize, every renderer) is **standard library only**.
  - `boto3` is needed only by the AWS collectors.
  - Import them inside functions, never at module top level, and raise a `RuntimeError` naming the extra to install (`pip install 'topomapper[aws]'`) if one is missing.
- **Read-only, always.**
  - The AWS collector wraps every client so only `Describe*`, `List*`, `Get*` and `Search*` calls can run. The one exception is STS `AssumeRole`, allowed only on the client that fetches credentials for other accounts.
- **Partial failure is normal.** An `AccessDenied` region or an unreachable account is recorded with `Topology.warn()`, and collection carries on. In the AWS collector each service in each region is guarded, so even an unexpected exception skips only that service. Only bad arguments are fatal.
- **No real data in the repo,** including in tests and fixtures.
  - Use documentation ranges (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`, `2001:db8::/32`) or RFC 1918 space.
  - Hostnames go under `example.net`, AWS account IDs are `123456789012` or `111122223333`, and serial numbers are made up.
- **Tests** use `unittest`, live in `tests/test_<area>.py`, and keep their fixtures in `tests/fixtures/<area>/`. They must skip, not fail, when an optional dependency is missing.
- **Exit status:** 0 for success, 1 only from `diff` ("differences found"), 2 for errors (like argparse).

## Module contract

Each command module exposes:

- `HELP`
- `PRODUCES_TOPOLOGY`
- `add_arguments(parser)`
- `run(args)`

`cli.py` wires them up as top-level commands (`topomapper aws`; the older `topomapper collect aws` is an unlisted alias). To every command that produces a topology it adds a repeatable `-o/--output`, whose extension picks the output (`.json` for the topology, `.drawio`/`.svg`/`.mmd`/`.dot` to draw it, refused before `run()` otherwise), and the drawing options `render` has. Modules don't handle output themselves. See the docstring in `cli.py`.

## IDs

IDs are deterministic, so the same thing collected twice, or from two sources, gets the same ID. Build them with `model.node_id(namespace, kind, *parts)`.

| Kind | ID |
| --- | --- |
| account | `aws:account:<account-id>` |
| region | `aws:region:<account-id>:<region>` |
| vpc, subnet, internet-gateway, egress-only-internet-gateway, nat-gateway, transit-gateway, vpn-gateway, customer-gateway, vpc-endpoint, instance | `aws:<prefix>:<resource-id>`, where the prefix follows AWS's own ID prefixes: `vpc`, `subnet`, `igw`, `eigw`, `nat`, `tgw`, `vgw`, `cgw`, `endpoint`, `instance`. Resource IDs are globally unique. |
| az | `aws:az:<vpc-id>:<az-name>` (an AZ is drawn *inside* a VPC) |
| dx-connection, dx-gateway | `aws:dx-connection:<dxcon-id>`, `aws:dx-gateway:<dxgw-id>` |
| AWS public services (end of a public VIF) | `ext:external:aws-public` |
| bedrock | `aws:bedrock:<account-id>:<region>` (the account's Bedrock models in that region, drawn as a group in the region) |
| bedrock-model | `aws:bedrock-model:<account-id>:<region>:<model-id>`; custom and imported models use `custom/<name>` and `imported/<name>` as the model ID |
| iam | `aws:iam:<account-id>` (the box holding an account's roles and users) |
| iam-role, iam-user | `aws:iam-role:<account-id>:<name>`, `aws:iam-user:<account-id>:<name>` |
| eks-cluster | `aws:eks-cluster:<account-id>:<region>:<name>` |
| organization, org-root, ou, org-policy | `aws:organization:<o-id>`, `aws:org-root:<r-id>`, `aws:ou:<ou-id>`, `aws:org-policy:<p-id>` |
| aws-resource | `aws:resource:<arn>` (from `--all-resources`). A resource the fallback found without an ARN is `aws:resource:<account-id>:<region>:<CloudFormation type>:<identifier>`. EC2 network resources keep the IDs above instead. A summary from `filter --max-per-type` is `aws:resource:<parent>/<type>`. |
| aws-service | `aws:service:<account-id>:<region or "global">:<ARN service name>` (the box holding one service's resources) |
| external | `ext:external:<what>`, e.g. `ext:external:internet` |

`topomapper aws` and `topomapper aws-org` give an account the same ID, so merging the two snapshots puts each account, with its regions and VPCs, inside its OU.

Both collectors describe themselves in `meta` the same way: `collector`, `collected_at`, and `accounts` (and, for `aws-org`, `ous`) as sorted lists of IDs. `topomapper aws` also records `regions` and `region_source`.

Link IDs come from `model.link_id()`, which `Topology.add_link()` calls for you. Undirected links are stored with `source < target`, and their ports are swapped to match.

## Containment (`parent`)

- **AWS:** `[organization > org-root > ou > … > ] account > region > vpc > az > subnet > (nat-gateway | instance)`. The organization levels come from `topomapper aws-org`; OUs nest to any depth. Organization policy nodes sit in the organization.
  - An internet gateway, egress-only gateway, VPC endpoint or VPN gateway attached to a VPC has that VPC as its parent.
  - Transit gateways, customer gateways, DX gateways and DX connections sit in their region.
  - A DX gateway is global, so it sits in its account.
  - With `--all-resources`, a resource sits in its subnet when AWS Config relates it to exactly one, in its VPC when it spans several subnets or names only a VPC, and otherwise in an `aws-service` box in its region (or in the account, for global services).
  - IAM roles and users are global, so they sit in one `iam` box per account, which the renderers draw in the account's top-right corner. A Bedrock group sits in its region, with its models inside it. An EKS cluster sits in its VPC, or in the region when that VPC wasn't collected.

## Node attributes (by kind)

Every attribute is optional, but renderers and merge rely on these names:

| Kind | Attributes |
| --- | --- |
| organization | `org_id`, `arn`, `feature_set`, `management_account_id` |
| org-root | `root_id`, `arn`, `policy_types` (enabled, short names: `scp`, `rcp`, `tag`, `backup`, `ai-opt-out`, `chatbot`, `declarative-ec2`), `policies` |
| ou | `ou_id`, `arn`, `path` (`Root/Workloads/Prod`), `policies`, `empty` (true when it holds no OUs or accounts) |
| org-policy | `policy_id`, `type` (short name), `aws_managed`, `description` |
| account | `account_id`, `name`, `collected` (true when `topomapper aws` reached it; after a merge, false for accounts only the organization listed); from `aws-org` also `status`, `joined_method`, `joined`, `management` (true for the management account), `delegated_admin_for` (service principals), `path`, `policies` (`{"scp": ["DenyRegions"], ...}`), and with `--include` `tags` and `email` |
| region | `region`, `empty` (true when nothing was found in it), `bedrock_models_shown`, `bedrock_models_offered`, `scp_denied` (services a service control policy blocks there, such as `["EC2"]`), `inventory_sources` (with `--all-resources`: which of `config`, `resource-explorer`, `cloud-control`, `tagging` it was read through) |
| vpc | `vpc_id`, `cidrs` (list), `ipv6_cidrs`, `is_default`, `account_id`, `region`, `tags` (dict, only with `--include tags`; this goes for every kind that has `tags`) |
| subnet | `subnet_id`, `cidr`, `ipv6_cidrs`, `az`, `public` (bool: its route table has a route to an internet gateway), `routes` (list of `{"destination", "target"}`), `tags` |
| internet-gateway, egress-only-internet-gateway | `gateway_id` |
| nat-gateway | `nat_gateway_id`, `public_ips`, `private_ips`, `connectivity` (`public`/`private`) |
| vpc-endpoint | `endpoint_id`, `service`, `endpoint_type` |
| transit-gateway | `tgw_id`, `asn`, `account_id`, `region`, `route_tables` (list of `{"id", "routes": [{"destination", "attachment", "type"}]}`) |
| vpn-gateway | `vgw_id`, `asn` |
| customer-gateway | `cgw_id`, `ip` (its public address), `asn`, `device_name` |
| dx-connection | `connection_id`, `location`, `bandwidth`, `state` |
| dx-gateway | `dxgw_id`, `asn` |
| instance | `instance_id`, `private_ips`, `public_ip`, `instance_type`, `state` |
| bedrock | `region`, `models_shown` (models drawn), `models_offered` (what Bedrock lists in the region), `models_in_use`, `granted_all_to` (principals with an Allow on every model), `provisioned_throughput` (list of `{"name", "model_units", "status"}`) |
| bedrock-model | `model_id`, `model_name`, `provider`, `input_modalities`, `output_modalities`, `inference_types` (`ON_DEMAND`, `PROVISIONED`, `INFERENCE_PROFILE`), `streaming`, `lifecycle`, `source` (`foundation`, `custom`, `imported`), `evidence` (`in use`, `granted`), `granted_to` (role, user or group names), `invocable` (false when the account isn't authorized, entitled or under an agreement), `base_model`, `arn` (custom and imported) |
| iam | `account_id`, `summary` ("12 roles, 3 users") |
| iam-role | `role_name`, `path`, `arn`, `account_id`, `created`, `last_used`, `aws_owned`, `trusted_accounts` (other accounts only), `trusts_own_account`, `trusted_principals` (list of `{"account", "type": root/role/user, "name"}`; named roles and users in the role's own account are kept, its own root is not), `trusted_services`, `trusted_federated`, `trusts_everyone`, `attached_policies`, `inline_policies`, `permissions_boundary`, `admin_access` (AdministratorAccess attached), `tags` |
| iam-user | `user_name`, `path`, `arn`, `account_id`, `created`, `password_last_used`, `groups`, `attached_policies`, `inline_policies`, `permissions_boundary`, `mfa_enabled`, `access_keys` (list of `{"status", "created"}`), `active_access_keys`, `tags` |
| aws-service | `service` (the ARN's service name), `region` (or `global`), `resources` (how many it holds) |
| aws-resource | `arn`, `resource_type` (CloudFormation's name when Config or Cloud Control gave one, else Resource Explorer's, such as `sqs:queue`), `resource_id` (Config's ID, which relationships refer to), `service`, `name`, `region`, `account_id`, `found_by` (`config`, `resource-explorer`, `cloud-control`, `tagging`), `relationships` (Config's, list of `{"relation", "type", "id"}`), `vpc_id`, `subnet_ids` (from Cloud Control), `tags`; a summary has `summarized` (how many it stands for) instead of the ARN |
| eks-cluster | `cluster_name`, `arn`, `version`, `status`, `platform_version`, `created`, `role_arn`, `endpoint_public_access`, `endpoint_private_access`, `public_access_cidrs`, `subnet_ids`, `security_group_ids`, `oidc_issuer`, `logging_enabled`, `secrets_encrypted`, `vpc_id`, `account_id`, `region`, `nodegroups` (list of `{"name", "status", "instance_types", "capacity_type", "ami_type", "version", "desired", "min", "max"}`), `fargate_profiles`, `addons`, `tags` |

Labels should be what a person calls the thing: the Name tag if present (`prod-vpc (vpc-0abc)`), otherwise the ID or hostname.

## Link kinds and attributes

| Kind | From → to | Attributes |
| --- | --- | --- |
| `tgw-attachment` | vpc / vpn / dx-gateway ↔ transit-gateway | `attachment_id`, `state`, `resource_type` |
| `tgw-peering` | transit-gateway ↔ transit-gateway | `attachment_id`, `state` |
| `vpc-peering` | vpc ↔ vpc | `peering_id`, `state` |
| `vgw-attachment` | vpn-gateway ↔ vpc | `state` |
| `vpn` | customer-gateway ↔ transit-gateway / vpn-gateway | `vpn_id`, `state`, `routing` (`bgp`/`static`), `tunnels` (list of `{"outside_ip", "inside_cidr", "status"}`), `attachment_id` (a TGW's VPN attachment is folded into this link rather than drawn separately) |
| `dx-vif` | dx-connection → dx-gateway / vpn-gateway / `ext:external:aws-public` (directed) | `vif_id`, `vif_type` (`private`/`transit`/`public`), `vlan`, `customer_address`, `amazon_address`, `asn`, `amazon_asn`, `state` |
| `dxgw-association` | dx-gateway ↔ transit-gateway / vpn-gateway | `association_id`, `state`, `allowed_prefixes` |
| `policy-attachment` | org-policy → org-root / ou / account (directed) | `type` |
| `related` | aws-resource → any node (directed), keyed by the relation | `relation`: an AWS Config relationship such as `Is associated with Role`. Only with `--include relationships`; ones that placed the resource, and the reverse `Contains …` ones, aren't drawn. |
| `iam-trust` | account / iam-role / iam-user → iam-role (directed) | `principal` (`root`, `role`, `user`): the role's trust policy lets that principal assume it. Only drawn when the trusted account was collected too. |

A DX gateway attached to a transit gateway appears twice: as a `tgw-attachment` (from the TGW's side) and a `dxgw-association` (from the DX side). Both are kept, because each carries its own state.

`filter --collapse` adds `collapsed: {kind: count}` to a node it folded children into, and `collapsed_links: N` to a link that several links were merged into.

A peer that wasn't collected (a VPC in another account, a TGW peer in another region) still gets a node, with `external: true` in its attributes, so the link has somewhere to go.

## Merge

`merge` unions topologies by ID, so an account from `aws-org` and the same account from `aws` become one node, and its regions land inside its OU. Where two inputs disagree about a node's kind or parent, the first wins and a warning names the node.

## Rendering

- **Formats:** `drawio`, `svg`, `mermaid` and `dot`.
- **draw.io:** written as **uncompressed** XML, so it is readable, diffable and redactable with `tools/ip-redactor`. It uses draw.io's built-in AWS 2021 (`mxgraph.aws4.*`) shapes and group styles, and nested containers for `parent`.
- **SVG:** built in, using the same layout engine. It does not need Graphviz.
- **Mermaid and DOT:** text, with subgraphs or clusters for containers.

## Modules

| Area | Files |
| --- | --- |
| Core | `model.py`, `cli.py` |
| AWS collector | `collectors/aws.py`; `collectors/aws_inventory.py` for `--all-resources`, and `aws_services.py` (service names and icons, shared with the renderers) |
| AWS Organizations collector | `collectors/aws_org.py` (shares the read-only client and sessions with `aws.py`) |
| Merge, diff, filter | `merge.py`, `diff.py`, `filter.py` |
| Layout and renderers | `render/*` |

Each area has its own `tests/test_<area>.py`. `tests/test_e2e.py` chains every stage through the CLI.
