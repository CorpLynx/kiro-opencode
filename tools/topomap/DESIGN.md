# topomap design

`topomap` gathers topology automatically from AWS and from network devices, merges the pieces, and draws them.

```
collect ─┬─ aws      boto3, read-only API calls            ┐
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
  - The AWS collector only calls `Describe*`, `List*` and `Get*`.
  - The crawler only runs commands from `parsers.COMMANDS`, and refuses anything else.
- **Partial failure is normal.** An unreachable device, an `AccessDenied` region or an unparseable file is recorded with `Topology.warn()`, and collection carries on. Only bad arguments are fatal.
- **No real data in the repo,** including in tests and fixtures.
  - Use documentation ranges (`192.0.2.0/24`, `198.51.100.0/24`, `203.0.113.0/24`, `2001:db8::/32`) or RFC 1918 space.
  - Hostnames go under `example.net`, AWS account IDs are `123456789012` or `111122223333`, and serial numbers are made up.
- **Tests** use `unittest`, live in `tests/test_<area>.py`, and keep their fixtures in `tests/fixtures/<area>/`. They must skip, not fail, when an optional dependency is missing.

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
| vpc, subnet, igw, nat, tgw, vgw, cgw, endpoint, instance | `aws:<kind>:<resource-id>` (resource IDs are globally unique) |
| az | `aws:az:<vpc-id>:<az-name>` (an AZ is drawn *inside* a VPC) |
| dx-connection, dx-gateway | `aws:dx-connection:<dxcon-id>`, `aws:dx-gateway:<dxgw-id>` |
| site | `net:site:<lower-case name>` |
| device | `net:device:<normalize_hostname(name)>`; use `facts.device_id()` |
| segment | `net:segment:<cidr>` |
| external | `ext:external:<what>`, e.g. `ext:external:internet` |

Link IDs come from `model.link_id()`, which `Topology.add_link()` calls for you. Undirected links are stored with `source < target`, and their ports are swapped to match.

## Containment (`parent`)

- **AWS:** `account > region > vpc > az > subnet > (nat-gateway | instance)`.
  - An internet gateway, egress-only gateway, VPC endpoint or VPN gateway attached to a VPC has that VPC as its parent.
  - Transit gateways, customer gateways, DX gateways and DX connections sit in their region.
  - A DX gateway is global, so it sits in its account.
- **Network:** `site > device` when the site is known. Otherwise devices sit at the top level. Segments sit at the top level.

## Node attributes (by kind)

Every attribute is optional, but renderers, merge and stitching rely on these names:

| Kind | Attributes |
| --- | --- |
| account | `account_id`, `name` |
| region | `region` |
| vpc | `vpc_id`, `cidrs` (list), `ipv6_cidrs`, `is_default`, `account_id`, `region`, `tags` (dict) |
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
| `vpn` | customer-gateway ↔ transit-gateway / vpn-gateway | `vpn_id`, `state`, `routing` (`bgp`/`static`), `tunnels` (list of `{"outside_ip", "inside_cidr", "status"}`) |
| `dx-vif` | dx-connection → dx-gateway / vpn-gateway | `vif_id`, `vif_type` (`private`/`transit`/`public`), `vlan`, `customer_address`, `amazon_address`, `asn`, `amazon_asn`, `state` |
| `dxgw-association` | dx-gateway ↔ transit-gateway / vpn-gateway | `association_id`, `state`, `allowed_prefixes` |
| `stitch` | device ↔ customer-gateway / dx-connection | `rule` (`cgw-ip`, `dx-vif`, `manual`), `address` |

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

## Ownership (initial build)

| Area | Files |
| --- | --- |
| Core (model, facts, CLI) | `model.py`, `facts.py`, `cli.py`, `__init__.py`, `tests/test_model.py`, `tests/test_facts.py` |
| AWS collector | `collectors/aws.py`, `tests/test_aws.py`, `tests/fixtures/aws/` |
| Parsers and offline collection | `parsers/*`, `collectors/files.py`, `tests/test_parsers.py`, `tests/test_files.py`, `tests/fixtures/devices/` |
| Live crawler | `collectors/devices.py`, `tests/test_devices.py`, `tests/fixtures/crawl/` |
| Renderers | `render/*`, `tests/test_render.py`, `tests/fixtures/render/` |
| Merge, diff, filter | `merge.py`, `diff.py`, `filter.py`, `tests/test_merge.py`, `tests/test_diff.py`, `tests/test_filter.py`, `tests/fixtures/merge/` |
