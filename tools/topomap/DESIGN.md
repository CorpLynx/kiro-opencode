# topomap design

`topomap` gathers topology automatically from AWS and from network devices, merges the pieces, and draws them.

```
collect ─┬─ aws      boto3, read-only API calls            ┐
         ├─ aws-org  the AWS Organization: OUs, accounts   │
         ├─ files    saved "show" output, config backups   ├─▶ topomap/1 JSON ─▶ merge ─▶ filter ─▶ render
         └─ devices  SSH crawl over CDP/LLDP, read-only    ┘                   diff (drift between snapshots)
```

Every stage reads and writes the same JSON graph (`topomap/model.py`), so stages compose with files or pipes, and snapshots can be committed and diffed.

## Ground rules

- **Python 3.9+.** Use `from __future__ import annotations` and `typing.Optional`/`List`/`Dict`. Don't use `X | None` outside annotations.
- **Optional dependencies:** the core (model, facts, parsers, files collector, merge/diff/filter, every renderer) is **standard library only**.
  - `boto3` is needed only by `collectors/aws.py`, and `netmiko` only by `collectors/devices.py`.
  - Import them inside functions, never at module top level, and raise a `RuntimeError` naming the extra to install (`pip install 'topomap[aws]'`) if one is missing.
- **Read-only, always.**
  - The AWS collector wraps every client so only `Describe*`, `List*`, `Get*` and `Search*` calls can run. The one exception is STS `AssumeRole`, allowed only on the client that fetches credentials for other accounts.
  - The crawler only runs commands from `parsers.COMMANDS`, and refuses anything else. netmiko's own session setup (`terminal length 0`) and, when autodetecting the platform, its probe commands still reach the device. Give `--platform` to avoid the probes.
- **Partial failure is normal.** An unreachable device, an `AccessDenied` region or an unparseable file is recorded with `Topology.warn()`, and collection carries on. In the AWS collector each service in each region is guarded, so even an unexpected exception skips only that service. Only bad arguments are fatal.
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

`cli.py` wires them up and adds `-o/--output` to commands that produce a topology. See the docstring in `cli.py`.

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

`collect aws` and `collect aws-org` give an account the same ID, so merging the two snapshots puts each account, with its regions and VPCs, inside its OU.

Both collectors describe themselves in `meta` the same way: `collector`, `collected_at`, and `accounts` (and, for `aws-org`, `ous`) as sorted lists of IDs. `collect aws` also records `regions` and `region_source`.
| site | `net:site:<lower-case name>` |
| device | `net:device:<normalize_hostname(name)>`; use `facts.device_id()` |
| segment | `net:segment:<cidr>` |
| external | `ext:external:<what>`, e.g. `ext:external:internet` |

Link IDs come from `model.link_id()`, which `Topology.add_link()` calls for you. Undirected links are stored with `source < target`, and their ports are swapped to match. Stitch links leave the device port out of the ID (`stitch|<aws end>|<device>|<address or "manual">`), so renaming an interface doesn't show up as a change in `diff`.

## Containment (`parent`)

- **AWS:** `[organization > org-root > ou > … > ] account > region > vpc > az > subnet > (nat-gateway | instance)`. The organization levels come from `collect aws-org`; OUs nest to any depth. Organization policy nodes sit in the organization.
  - An internet gateway, egress-only gateway, VPC endpoint or VPN gateway attached to a VPC has that VPC as its parent.
  - Transit gateways, customer gateways, DX gateways and DX connections sit in their region.
  - A DX gateway is global, so it sits in its account.
  - IAM roles and users are global, so they sit in one `iam` box per account, which the renderers draw in the account's top-right corner. A Bedrock group sits in its region, with its models inside it. An EKS cluster sits in its VPC, or in the region when that VPC wasn't collected.
- **Network:** `site > device` when the site is known. Otherwise devices sit at the top level. Segments sit at the top level.

## Node attributes (by kind)

Every attribute is optional, but renderers, merge and stitching rely on these names:

| Kind | Attributes |
| --- | --- |
| organization | `org_id`, `arn`, `feature_set`, `management_account_id` |
| org-root | `root_id`, `arn`, `policy_types` (enabled, short names: `scp`, `rcp`, `tag`, `backup`, `ai-opt-out`, `chatbot`, `declarative-ec2`), `policies` |
| ou | `ou_id`, `arn`, `path` (`Root/Workloads/Prod`), `policies`, `empty` (true when it holds no OUs or accounts) |
| org-policy | `policy_id`, `type` (short name), `aws_managed`, `description` |
| account | `account_id`, `name`, `collected` (true when `collect aws` reached it; after a merge, false for accounts only the organization listed); from `aws-org` also `status`, `joined_method`, `joined`, `management` (true for the management account), `delegated_admin_for` (service principals), `path`, `policies` (`{"scp": ["DenyRegions"], ...}`), and with `--include` `tags` and `email` |
| region | `region`, `empty` (true when nothing was found in it), `bedrock_models_shown`, `bedrock_models_offered`, `scp_denied` (services a service control policy blocks there, such as `["EC2"]`) |
| vpc | `vpc_id`, `cidrs` (list), `ipv6_cidrs`, `is_default`, `account_id`, `region`, `tags` (dict, only with `--include tags`; this goes for every kind that has `tags`) |
| subnet | `subnet_id`, `cidr`, `ipv6_cidrs`, `az`, `public` (bool: its route table has a route to an internet gateway), `routes` (list of `{"destination", "target"}`), `tags` |
| internet-gateway, egress-only-internet-gateway | `gateway_id` |
| nat-gateway | `nat_gateway_id`, `public_ips`, `private_ips`, `connectivity` (`public`/`private`) |
| vpc-endpoint | `endpoint_id`, `service`, `endpoint_type` |
| transit-gateway | `tgw_id`, `asn`, `account_id`, `region`, `route_tables` (list of `{"id", "routes": [{"destination", "attachment", "type"}]}`) |
| vpn-gateway | `vgw_id`, `asn` |
| customer-gateway | `cgw_id`, `ip` (its public address, the key used for stitching), `asn`, `device_name` |
| dx-connection | `connection_id`, `location`, `bandwidth`, `state` |
| dx-gateway | `dxgw_id`, `asn` |
| instance | `instance_id`, `private_ips`, `public_ip`, `instance_type`, `state` |
| site | `name` |
| device | `vendor`, `platform`, `model`, `os_version`, `serial`, `mgmt_ip`, `role` (see `facts.ROLES`), `interfaces` (list of `{"name", "addresses": ["10.0.0.1/30"], "description", "status", "vrf"}`), `discovered` (`"neighbor"` if only seen as someone's CDP/LLDP neighbour), `sources` |
| segment | `subnet` |
| bedrock | `region`, `models_shown` (models drawn), `models_offered` (what Bedrock lists in the region), `models_in_use`, `granted_all_to` (principals with an Allow on every model), `provisioned_throughput` (list of `{"name", "model_units", "status"}`) |
| bedrock-model | `model_id`, `model_name`, `provider`, `input_modalities`, `output_modalities`, `inference_types` (`ON_DEMAND`, `PROVISIONED`, `INFERENCE_PROFILE`), `streaming`, `lifecycle`, `source` (`foundation`, `custom`, `imported`), `evidence` (`in use`, `granted`), `granted_to` (role, user or group names), `invocable` (false when the account isn't authorized, entitled or under an agreement), `base_model`, `arn` (custom and imported) |
| iam | `account_id`, `summary` ("12 roles, 3 users") |
| iam-role | `role_name`, `path`, `arn`, `account_id`, `created`, `last_used`, `aws_owned`, `trusted_accounts` (other accounts only), `trusts_own_account`, `trusted_principals` (list of `{"account", "type": root/role/user, "name"}`; named roles and users in the role's own account are kept, its own root is not), `trusted_services`, `trusted_federated`, `trusts_everyone`, `attached_policies`, `inline_policies`, `permissions_boundary`, `admin_access` (AdministratorAccess attached), `tags` |
| iam-user | `user_name`, `path`, `arn`, `account_id`, `created`, `password_last_used`, `groups`, `attached_policies`, `inline_policies`, `permissions_boundary`, `mfa_enabled`, `access_keys` (list of `{"status", "created"}`), `active_access_keys`, `tags` |
| eks-cluster | `cluster_name`, `arn`, `version`, `status`, `platform_version`, `created`, `role_arn`, `endpoint_public_access`, `endpoint_private_access`, `public_access_cidrs`, `subnet_ids`, `security_group_ids`, `oidc_issuer`, `logging_enabled`, `secrets_encrypted`, `vpc_id`, `account_id`, `region`, `nodegroups` (list of `{"name", "status", "instance_types", "capacity_type", "ami_type", "version", "desired", "min", "max"}`), `fargate_profiles`, `addons`, `tags` |

Labels should be what a person calls the thing: the Name tag if present (`prod-vpc (vpc-0abc)`), otherwise the ID or hostname.

## Link kinds and attributes

| Kind | From → to | Attributes |
| --- | --- | --- |
| `physical` | device ↔ device | `source_port`, `target_port`, `protocols` (`["cdp", "lldp"]`), `subnets` |
| `l3` | device ↔ device | `subnet`, `addresses` (`{node_id: ip}`), `vrf` |
| `segment-member` | device → segment | `source_port`, `address`, `vrf` |
| `tgw-attachment` | vpc / vpn / dx-gateway ↔ transit-gateway | `attachment_id`, `state`, `resource_type` |
| `tgw-peering` | transit-gateway ↔ transit-gateway | `attachment_id`, `state` |
| `vpc-peering` | vpc ↔ vpc | `peering_id`, `state` |
| `vgw-attachment` | vpn-gateway ↔ vpc | `state` |
| `vpn` | customer-gateway ↔ transit-gateway / vpn-gateway | `vpn_id`, `state`, `routing` (`bgp`/`static`), `tunnels` (list of `{"outside_ip", "inside_cidr", "status"}`), `attachment_id` (a TGW's VPN attachment is folded into this link rather than drawn separately) |
| `dx-vif` | dx-connection → dx-gateway / vpn-gateway / `ext:external:aws-public` (directed) | `vif_id`, `vif_type` (`private`/`transit`/`public`), `vlan`, `customer_address`, `amazon_address`, `asn`, `amazon_asn`, `state` |
| `dxgw-association` | dx-gateway ↔ transit-gateway / vpn-gateway | `association_id`, `state`, `allowed_prefixes` |
| `policy-attachment` | org-policy → org-root / ou / account (directed) | `type` |
| `iam-trust` | account / iam-role / iam-user → iam-role (directed) | `principal` (`root`, `role`, `user`): the role's trust policy lets that principal assume it. Only drawn when the trusted account was collected too. |
| `stitch` | device ↔ customer-gateway / dx-connection | `rule` (`cgw-ip`, `dx-vif`, `manual`), `address`, the device-side port |

A DX gateway attached to a transit gateway appears twice: as a `tgw-attachment` (from the TGW's side) and a `dxgw-association` (from the DX side). Both are kept, because each carries its own state.

`filter --collapse` adds `collapsed: {kind: count}` to a node it folded children into, and `collapsed_links: N` to a link that several links were merged into.

A peer that wasn't collected (a VPC in another account, a TGW peer in another region) still gets a node, with `external: true` in its attributes, so the link has somewhere to go.

## Stitching (merge)

`merge` unions topologies by ID, then links on-prem devices to the AWS edge:

1. **`cgw-ip`:** a device interface address (without its prefix length) equals a customer gateway's `ip`.
2. **`dx-vif`:** a device interface address equals the IP part of a VIF's `customer_address`. The stitch link goes from the device to that VIF's DX connection.
3. **`manual`:** `--stitch NODE_ID=NODE_ID` pairs, for gateways behind NAT.

## Rendering

- **Formats:** `drawio`, `svg`, `mermaid` and `dot`.
- **draw.io:** written as **uncompressed** XML, so it is readable, diffable and redactable with `tools/ip-redactor`. It uses draw.io's built-in AWS 2021 (`mxgraph.aws4.*`) shapes and group styles, and nested containers for `parent`.
- **SVG:** built in, using the same layout engine. It does not need Graphviz.
- **Mermaid and DOT:** text, with subgraphs or clusters for containers.

## Modules

| Area | Files |
| --- | --- |
| Core | `model.py`, `facts.py`, `cli.py` |
| AWS collector | `collectors/aws.py` |
| AWS Organizations collector | `collectors/aws_org.py` (shares the read-only client and sessions with `aws.py`) |
| Parsers and offline collection | `parsers/*`, `collectors/files.py` |
| Live crawler | `collectors/devices.py` |
| Merge, diff, filter | `merge.py`, `diff.py`, `filter.py` |
| Layout and renderers | `render/*` |

Each area has its own `tests/test_<area>.py`. `tests/test_e2e.py` chains every stage through the CLI.

### Platform notes

- **Junos:** `show lldp neighbors detail` isn't a valid Junos command (only the per-interface form is), so the crawler runs the `show lldp neighbors` summary. That summary has no management addresses, so the crawler can't follow Junos neighbours by IP; saved per-interface detail output is still parsed.
- **`show ip interface brief`:** IOS and NX-OS print no masks, so it only contributes interface names and status; addresses come from `show ip interface` or the configuration.
