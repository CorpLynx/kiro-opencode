# topomap: network and AWS topology, gathered and drawn automatically

`topomap` builds topology diagrams from the network itself instead of from someone's memory. It collects from three kinds of source:

- **AWS:** read-only API calls across accounts and regions, including the AWS Organization itself (OUs, accounts, SCPs and other policies), collected from the management account, and with flags the Bedrock models each account can use, its custom IAM roles and users, EKS clusters, or every resource in the account.
- **Network devices:** an SSH crawl that follows CDP/LLDP neighbours.
- **Files you already have:** saved `show` output and config backups, from RANCID, Oxidized or a folder of captures.

It links on-prem devices to their AWS VPN and Direct Connect edges, then draws the result as **draw.io** (with the official AWS and Cisco icons), **SVG**, **Mermaid** or **Graphviz DOT**.

![A hybrid lab: an AWS account with a transit gateway, VPCs and subnets, stitched over a site-to-site VPN to an on-prem Junos edge router, NX-OS core pair, Catalyst and Arista switches](docs/hybrid.png)

<sub>Drawn by `topomap render` from the test fixtures (mocked AWS plus made-up device output).</sub>

```
collect ─┬─ aws      boto3, read-only                       ┐
         ├─ aws-org  OUs, accounts, policies                │
         ├─ files    saved "show" output / config backups   ├─▶ topology JSON ─▶ merge ─▶ filter ─▶ render
         ├─ devices  SSH crawl over CDP/LLDP, read-only     │                     diff (drift between snapshots)
         └─ scan     nmap host and port discovery           ┘
```

Every stage reads and writes the same JSON, so stages chain through files or pipes. The JSON is sorted with stable IDs, so snapshots can be committed to git and compared.

## Install

```sh
pip install './tools/topomap[all]'    # AWS + SSH crawling
pip install ./tools/topomap           # files, merge, diff, filter, render: no dependencies
```

| Extra | Adds | Needs |
| --- | --- | --- |
| *(none)* | `collect files`, `merge`, `diff`, `filter`, `render` (all formats) | Python 3.9+ standard library |
| `aws` | `collect aws` | `boto3` |
| `devices` | `collect devices` | `netmiko` |

## Quick start

```sh
# 1. Gather
topomap collect aws-org --profile management -o org.json        # the organization chart
topomap collect aws --profile prod --region us-east-1 --region eu-west-1 -o aws.json
topomap collect files ~/rancid/configs ~/captures --l3-exclude 192.168.0.0/24 -o net.json
#    or crawl live:
TOPOMAP_USERNAME=netops topomap collect devices --seed 10.0.0.1 --allow 10.0.0.0/16 --ask-pass -o net.json

# 2. Combine: accounts land in their OUs; on-prem devices are linked to their
#    AWS customer gateways and DX VIFs
topomap merge org.json aws.json net.json -o all.json
topomap render org.json -o organization.drawio                 # or just the org chart

# 3. Draw (format from the extension)
topomap render all.json -o network.drawio
topomap filter all.json --aws-only --collapse vpc | topomap render - -o aws-overview.svg

# 4. Later: what changed?
topomap diff last-month.json all.json      # exit status 1 if anything did
```

## Collecting

### AWS (`collect aws`)

- **What it collects:**
  - accounts, regions and VPCs;
  - subnets, drawn inside their AZs, with public/private detection and routes;
  - internet, egress-only and NAT gateways;
  - transit gateways, with their route tables and attachments;
  - VPC peering, VPN gateways and customer gateways;
  - VPN connections, with tunnels;
  - Direct Connect connections, gateways, virtual interfaces and associations.
  - With `--include instances,endpoints`, also EC2 instances and VPC endpoints.
  - Tags are left out by default, since they are free text (project names, owners). Add `--include tags` to keep them. A resource's Name tag still labels it.
  - Every region enabled for the account is walked unless you pass `--region`. `meta.region_source` records how the list was chosen: `--region`, `describe-regions`, `fallback` (the region listing was denied, so `us-east-1` only) or `mixed`.
- **Read-only, enforced in code.** Every boto3 client is wrapped so only `Describe*`/`List*`/`Get*`/`Search*` calls can run. The only exception is STS `AssumeRole`, on the client that fetches credentials for other accounts.
- **Several accounts:**
  - `--profile` (repeatable) uses your own profiles.
  - `--role-arn` (repeatable) assumes roles from a hub account. With it, only the accounts behind the roles are collected.
  - `--org-role-name NAME` assumes `role/NAME` in every active account of your AWS Organization.
- **Speed:** regions and accounts are collected in parallel (`--workers`).
- **Failures:** a denied region or service becomes one warning and the rest is still collected.
- **Regions blocked by an SCP:** when a service control policy explicitly denies EC2, EKS, Bedrock or Direct Connect in a region, that is recognised by the error text, reported once per service and region, and recorded on the region as `scp_denied`. The diagram labels it `SCP blocks EC2, EKS`, and `filter --hide-empty` drops it.
- **Peers outside the collected estate** (a VPC in another account, a TGW peer) get a dashed placeholder.
- VPN pre-shared keys and customer gateway configurations are never read.

### GovCloud and other partitions

topomap works in the AWS GovCloud (US) partition. Credentials only work in their own partition, so a GovCloud profile is separate from a commercial one.

```sh
topomap collect aws-org --profile gov-management -o topomap-out/gov-org.json
topomap collect aws --profile gov-management --region us-gov-west-1 --region us-gov-east-1 \
    --iam -o topomap-out/gov.json
```

- **Home region:** with no `--region` and no region in the profile, topomap tries the commercial, GovCloud and China starting regions in turn, so the partition is found from the credentials. Give `--region us-gov-west-1` to skip the probing.
- **Regions:** `describe-regions` lists `us-gov-east-1` and `us-gov-west-1`. If that listing is denied, the fallback is `us-gov-west-1`.
- **Service lookups:** whether Bedrock or EKS exists in a region is checked in that region's own partition. GovCloud has both in `us-gov-west-1` and `us-gov-east-1`.
- **ARNs:** `--role-arn` and `--org-role-name` use the partition the credentials belong to (`arn:aws-us-gov:...`).
- **Merging:** account IDs in GovCloud and commercial are different accounts, so merging both into one diagram is safe. Nothing links the two partitions.
- **Tested with an emulator only.** The GovCloud code paths are exercised against moto in `us-gov-west-1` and with stubbed partition lookups, never against a real GovCloud account. Please report anything that behaves differently there. Bedrock availability in GovCloud follows the same API but I haven't seen real GovCloud answers.

### Bedrock, IAM and EKS (`--ai`, `--iam`, `--eks`)

Three flags on `collect aws` add what the network collection doesn't cover. Each is off by default, read-only, and works across every account `collect aws` reaches (`--org-role-name` for the whole organization).

| Flag | Adds | Needs (read-only) |
| --- | --- | --- |
| `--ai` | A **Bedrock** group in each account and region where Bedrock exists. It holds a node for each foundation model that is **in use** (CloudWatch has Bedrock metrics for it in the last 14 days) or **granted** (an IAM policy in that account allows invoking it by name or pattern), plus the account's custom and imported models. A model nobody can call, because its agreement isn't in place, is marked `not invocable`. A policy granting every model is recorded on the box (`granted_all_to`), not drawn as hundreds of nodes. The list of models Bedrock offers in the region is only a count. Regions without Bedrock aren't asked. | `bedrock:ListFoundationModels`, `GetFoundationModelAvailability`, `ListCustomModels`, `ListImportedModels`, `ListProvisionedModelThroughputs`, `cloudwatch:ListMetrics`, `iam:GetAccountAuthorizationDetails` |
| `--iam` | One node per **custom IAM role** and **user**, in an **IAM box** that is always drawn in the account's top-right corner. For a role: who may assume it (accounts, services, federated providers, or anyone), policy names, permissions boundary, last use. For a user: groups, policy names, whether MFA is on, and the status and age of access keys. Roles that trust a principal in another collected account get a drawn **trust link**. Service-linked roles and Identity Center roles are left out unless you add `--iam-all`. No policy documents, key IDs or credentials are read. | `iam:GetAccountAuthorizationDetails` (falls back to `ListRoles` and `ListUsers`), `ListMFADevices`, `ListAccessKeys` |
| `--eks` | An **EKS cluster** node in its VPC: version, status, whether the API endpoint is public or private, logging, IAM role, OIDC issuer, node groups, Fargate profiles and add-ons. Pods and namespaces need the Kubernetes API and aren't collected. | `eks:ListClusters`, `DescribeCluster`, `ListNodegroups`, `DescribeNodegroup`, `ListFargateProfiles`, `DescribeFargateProfile`, `ListAddons` |

Add `--skip-network` to leave out the VPC, gateway, TGW, VPN and Direct Connect collection, for a faster run when you only want these. Regions with nothing to show are then dropped from the output.

**Why "in use or granted" and not "enabled":** Bedrock now switches most models on for every account, and its availability API says the same about all of them (an Anthropic model additionally needs a use-case form and, for some providers, an agreement). So availability can't tell one account from another. Usage and IAM grants can. Grants mean "an Allow statement exists": deny statements and permission boundaries aren't evaluated, so a grant doesn't prove a call would succeed. Usage only reaches back as far as CloudWatch keeps it (about two weeks).

Bedrock access was written against the API's documentation, checked once against a real account, and tested with stubbed answers, because the test AWS emulator has no Bedrock. Please report anything that doesn't match what the console shows.

An organization-wide picture of which accounts can use which models:

```sh
topomap collect aws-org --profile management -o topomap-out/org.json
topomap collect aws --profile management --org-role-name OrganizationAccountAccessRole \
    --ai --skip-network -o topomap-out/ai.json
topomap merge topomap-out/org.json topomap-out/ai.json -o topomap-out/org-ai.json
topomap filter topomap-out/org-ai.json --kind bedrock-model --hide-empty -o topomap-out/models.json
topomap render topomap-out/models.json -o topomap-out/models.svg
```

`filter --kind bedrock-model` keeps the models and everything above them (region, account, OU, organization). The same works for `--kind iam-role --kind iam-user` (an IAM view with trust links) and `--kind eks-cluster`. Roles, users and cluster names identify an estate, so `topomap anonymize` replaces them (and `--strict` drops everything else but a short list of facts).

### Every resource in an account (`--all-resources`)

`collect aws --all-resources` draws everything in the account, not only the network: every Lambda function, bucket, queue, table, key, role, log group and the rest, each with its service's AWS icon.

```sh
topomap collect aws --profile prod --all-resources -o topomap-out/everything.json
topomap filter topomap-out/everything.json --max-per-type 10 | topomap render - -o topomap-out/everything.drawio
```

- **Where the list comes from.** AWS has no single "list everything" call, so each region is asked through whichever inventory service the account has turned on, and the answers are combined by ARN:
  - **AWS Config**, if a recorder is recording (Control Tower turns it on in governed regions). Config also gives each resource's relationships, which is what puts a resource in its VPC or subnet.
  - **Resource Explorer**, if the region has an index and a default view. It lists untagged resources too.
  - If neither is on in a region, a **fallback** lists about fifty common resource types with Cloud Control, plus anything tagged, through the tagging API. That can miss types, so there's one warning per account naming the regions. Turning on Resource Explorer (one click, all regions, no charge) gives the complete list.
  - Each region records what it used in `inventory_sources`.
- **Where each resource is drawn:**
  - A resource the network collection already draws (VPC, subnet, instance, gateway) keeps its node, and IAM roles and EKS clusters from `--iam` and `--eks` keep theirs.
  - When Config says a resource is in one subnet, it's drawn in that subnet. When it spans several subnets (a Lambda function, a load balancer) or names only a VPC (a security group), it's drawn in the VPC.
  - Everything else goes in a box for its service in its region ("Lambda · 12 resources"), or in the account for global services such as IAM, CloudFront and Route 53.
- **Relationships:** `--include relationships` also draws Config's relationships between resources ("Is associated with Role") as thin dashed arrows. Relationships that placed a resource aren't drawn twice.
- **Big accounts:** the JSON keeps every resource. `filter --max-per-type N` draws a box's resources of one type as one node (`286 × certificate`) wherever there are more than N, which keeps a few thousand resources readable.
- **Permissions:** the AWS-managed `ReadOnlyAccess` policy covers all of it: `config:DescribeConfigurationRecorderStatus` and `config:SelectResourceConfig`, `resource-explorer-2:GetIndex`, `GetDefaultView` and `ListResources`, and for the fallback `cloudformation:ListResources` (with the services' own list permissions) and `tag:GetResources`. `SelectResourceConfig` is read-only but isn't named `Describe`/`List`/`Get`/`Search`, so it's allowed by name on the Config client only.
- **Without `--skip-network`** you get the full VPC picture with resources placed in it. With `--skip-network`, VPCs and subnets come from the inventory itself and are still drawn as VPCs and subnets.
- **Not collected:** resource configurations (only identity, type, tags and relationships), and resources Config and Resource Explorer don't record or index. Config's compliance and inventory reports aren't resources and are left out.

### AWS Organizations (`collect aws-org`)

![An organization chart: the root with its SCP, the management account, and Infrastructure, Sandbox, Security and Workloads OUs (Workloads holding Prod and NonProd), each OU listing its SCPs and accounts](docs/organization.png)

<sub>Drawn from a mocked organization.</sub>

- **Run it against the management account,** or an account that is a delegated administrator for AWS Organizations. Only those can list the whole organization. A member account gets a clear error saying so.
- **Permissions:** the AWS-managed `AWSOrganizationsReadOnlyAccess` policy is enough. Like `collect aws`, it can only call `Describe*`/`List*`/`Get*`, plus `AssumeRole` with `--role-arn`.
- **What it collects:**
  - the organization, its root and the enabled policy types;
  - every OU, nested to any depth, with its path (`Root/Workloads/Prod`);
  - every account, with its name, status (suspended accounts are labelled), how and when it joined, and which one is the management account;
  - delegated administrators and the services they administer;
  - organization policies (SCPs, RCPs, tag, backup, AI opt-out, chatbot and declarative policies) on the root, OUs and accounts they're attached to.
- **Policies** (`--policies`):
  - `attrs` (default): policies are attributes, and SCPs are written under each OU.
  - `nodes`: also draws each policy as a box with lines to its targets.
  - `none`: leaves policies out.
  - AWS-managed policies such as `FullAWSAccess` are attached everywhere, so they're hidden unless you add `--include aws-managed-policies`.
- **Extras** (`--include`):
  - `tags`: account tags, one API call per account.
  - `emails`: account email addresses. These are personal data, so they're off by default.
- **Combining with network data:** `collect aws` gives each account the same ID, so `merge org.json aws.json` draws each account inside its OU, with its regions and VPCs inside the account. After a merge, an account that `collect aws` could not reach is labelled "not collected", an empty OU "empty" and a region with nothing in it "no resources", so a bare box is never mistaken for an unscanned one. An account with nothing collected inside it is drawn as an account icon; one holding VPCs is drawn as a container.

### Saved output and configs (`collect files`)

- **Platforms:** Cisco IOS/IOS-XE, Cisco NX-OS, Arista EOS and Juniper Junos.
- **What it reads:**
  - captures with prompts (`core1#show cdp neighbors detail`, `netops@edge1> show lldp neighbors`) or with banners (`===== show version =====`);
  - abbreviated commands (`sh cdp nei det`);
  - running configs: IOS/NX-OS/EOS style, and Junos in both `set` and curly-brace style.
- **Commands understood:** `show version`, `show cdp neighbors detail`, `show lldp neighbors [detail]`, `show ip interface [brief]` / `show ipv6 interface`, `show interfaces terse`, `show hostname`, `show chassis hardware`, and configurations.
- **Several files per device:** a capture and a config for the same device are merged into one.
- **Sites:** `--site NAME`, or `--site-map sites.csv` (`hostname,site`, with `*` wildcards).
- **Layer 3:** a subnet shared by two devices becomes a link, or is added to their cable if there is one. A subnet shared by three or more becomes a segment. Use `--l3-exclude` for the management network, which otherwise connects everything to everything.

### Live crawl (`collect devices`)

- **What it does:** logs into `--seed` devices (or an `--inventory` CSV/JSON) over SSH and runs the same read-only commands. It then follows CDP/LLDP neighbours breadth-first.
- **Safety:**
  - **It only crawls beyond the seeds into `--allow` ranges.** Without `--allow`, only the seeds are contacted.
  - Depth is limited by `--max-depth`, and neighbours that look like phones, APs or servers are skipped (`--skip-role`).
  - Every command is checked against the per-platform allowlist before anything is sent. Only `show` commands are allowed, with no pipes.
  - **Passwords** come only from `$TOPOMAP_PASSWORD` or an `--ask-pass` prompt, never from the command line or an inventory file. Keys in the SSH agent or given with `--key-file` work too.
  - `--strict-host-keys` refuses devices whose host key isn't in `~/.ssh/known_hosts`. Without it, netmiko accepts any key.
- **Autodetection:** when the platform isn't given, it is autodetected with netmiko, which sends a few probe commands of its own. Set `--platform` or put platforms in the inventory to avoid that.
- `--save-raw DIR` keeps each device's output (mode 0600) in the format `collect files` reads, so a crawl can be replayed offline.

### Home and lab networks (`collect scan`)

Finds the hosts and open ports on a network with [nmap](https://nmap.org) and draws them: one segment per network, every host linked to it, the default gateway as the router, and each host's services and a guessed type (gateway, server, NAS, printer, camera, access point, media player…) on its label.

```sh
topomap collect scan 192.168.1.0/24 --save-xml topomap-out/scan -o topomap-out/lan.json
topomap render topomap-out/lan.json -o topomap-out/lan.svg
```

- **Two nmap runs:** a host sweep (`-sn`), then `--top-ports 1000` with service detection (`-sV`) on the hosts that answered. `--ports 22,80,1-1024`, `--no-ports`, `--no-service-detection`, `--full-probes` (every service probe: exact but slow on routers), `--no-dns`, `--timing 0-5`, `--exclude ADDR` and `--os` (needs root) change that.
- **Root or not:** with `sudo`, nmap finds hosts by ARP and reads MAC addresses itself. Without it, hosts are found with TCP pings plus this machine's ARP cache after the sweep (`--no-arp-cache` turns that off), and vendors come from nmap's own MAC prefix list.
- **Safety:** only private, link-local and CGNAT (Tailscale) space is scanned unless you add `--allow-public`, and at most 65,536 addresses per run.
- **Offline:** `--from-xml FILE` (repeatable) redraws saved `nmap -oX` output without scanning.
- Needs the `nmap` program (`brew install nmap`, `apt install nmap`, `pacman -S nmap`), not a Python package.

## Combining, trimming and comparing

- **`merge`** unions topologies, then stitches each on-prem device to:
  - an AWS customer gateway whose IP is one of the device's addresses;
  - the Direct Connect connection of a VIF whose customer address is one of the device's addresses;
  - anything you pair by hand with `--stitch device=node`, for gateways behind NAT. Gateways that match nothing are listed in a warning.
- **`filter`** shrinks big estates down to a readable picture. It takes `--aws-only` / `--network-only`, `--kind`, `--exclude-kind az` (children move up), `--collapse vpc` (subnets fold into their VPC, and links are re-pointed), `--max-per-type N` (more than N resources of one type in a box become one counted node), `--around core1 --depth 2`, `--match REGEX` and `--hide-empty` (drops regions and OUs in which nothing was found).
- **`diff`** shows what was added, removed and changed, down to the attribute (`interfaces[Gi1/0/1].addresses: +10.0.0.9/30`). It writes text or JSON, and exits 1 when anything changed, so it can watch for drift in CI.

## Drawing (`render`)

| Format | Notes |
| --- | --- |
| `drawio` | Opens in draw.io / diagrams.net and can be exported to Visio. Uses draw.io's built-in AWS 2021 and Cisco 19 shapes, with nested containers (account › region › VPC › AZ › subnet; site › device). Written as uncompressed XML, so it diffs cleanly and can be redacted as text. |
| `svg` | Standalone, uses the same layout and needs no Graphviz. |
| `mermaid` | Paste into GitHub or GitLab Markdown, Notion, etc. |
| `dot` | For Graphviz (`dot -Tpng`). |

- **Layout:** read top to bottom: internet, then edge gateways and firewalls, then routers, core switches, access switches and endpoints.
- **Output is deterministic:** the same input gives byte-identical output.
- **Options:** `--direction LR` reads left to right, `--title` adds a title, and `--no-link-labels` drops port and subnet labels.

### Sharing and committing output safely

Snapshots and diagrams carry account IDs, names, tags and addresses from the real estate. Two things help:

- **Keep real output out of git.** Write it under `topomap-out/`, which the repo ignores (`-o topomap-out/all.json`; the folder is created for you). Real output is written readable by you only (mode 0600). If it lands inside a git repository that doesn't ignore it, topomap prints a note. That covers `-o`, a redirected stdout (`> out.json`) and a `--save-raw` folder.
- **`anonymize` makes a shareable copy.** Render from the anonymized JSON and the diagram is safe too:

```sh
topomap anonymize topomap-out/all.json -o sample.json
topomap render sample.json -o sample.svg
```

  It replaces account, organization, OU and resource IDs (same prefix and length), names and tag values (`account-1`, `ou-2`), descriptions, serial numbers, source file names, e-mail addresses and MAC addresses. The same real value always gets the same fake, and parent/child structure is kept. Nothing maps back, because no table or key is written. If any original ID is still in the output, there's a warning.
  - **Addresses:** `--ips ipredact` (the default) runs the addresses through [`tools/ip-redactor`](../ip-redactor), keeping subnets and containment true. Install it first (`pip install ./tools/ip-redactor`); without it, the command stops with that instruction. `--ips public` needs nothing: it moves only public addresses into the documentation ranges and leaves private ones as they are. `--ips keep` leaves all of them. `--ip-key-file` gives the same addresses on every run.
  - **`--keep-names`** leaves names and tag values alone; IDs and addresses are still replaced.
  - **`--strict`** also drops every attribute that isn't on the list of known, non-free-text ones (tags, descriptions, serials, anything unfamiliar). Use it when you don't know what a field might contain.
  - **Checks:** the output is searched for the original IDs, names and public addresses, and anything that survived is a warning. Fakes never equal a real value, and link order stays canonical, so `diff` and `merge` work on anonymized snapshots.

The text outputs (draw.io, SVG, Mermaid, DOT) can also be run through `ipredact` directly, but that doesn't touch IDs or names.

## Limits

- **Lines are straight.** In dense areas they can cross icons and other containers, and port labels can overlap. Tidy up in draw.io if it matters.
- **Junos crawling:** Junos has no `show lldp neighbors detail` for all interfaces, so the crawler can't follow Junos neighbours by address. They are still drawn.
- **Not parsed yet:** CDP/LLDP summary tables on Cisco and Arista (only the `detail` forms), Junos `groups`/`apply-groups`, and SNMP.
- **AWS resources not collected:** DX LAGs, TGW Connect peers and Cloud WAN; load balancers, security groups and everything else only with `--all-resources`. From Organizations: policy contents (only names and attachments), and SCP effects on accounts.
- **Bedrock, IAM and EKS tests use emulators and stubs.** moto has no Bedrock API, so `--ai` is tested with stubbed answers; IAM and EKS run against moto. All three have also been run against a real lab organization, but only that one, so please report anything that looks wrong.
- **Trust links are drawn at collection time,** between accounts collected in the same run. Merging snapshots from separate runs doesn't add links between them. A role that trusts an account outside the run keeps it in `trusted_accounts` but gets no link.
- **IAM scope:** policy documents, permission analysis and credentials are never read, so "admin" means the AdministratorAccess managed policy is attached, not that the effective permissions were computed.
- **`--all-resources` is tested with stubbed Config, Resource Explorer and Cloud Control answers** (the emulator has none of them), not yet against a real account. Please report resources that land in the wrong place.
- **Untested against real equipment:** the device parsers were written from known output formats and tested against made-up captures, not real devices. Please report output that doesn't parse.

## Development

```sh
pip install './tools/topomap[test]'
cd tools/topomap && python3 -m unittest discover -s tests
```

[DESIGN.md](DESIGN.md) is the contract between the stages: IDs, kinds, attributes, containment and the stitching rules. `tests/test_e2e.py` runs every stage through the CLI, from mocked AWS plus the device fixtures in `tests/fixtures/devices/` (a made-up multi-vendor lab) to every output format.
