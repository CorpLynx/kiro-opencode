# topomapper: network and AWS topology, gathered and drawn automatically

`topomapper` builds topology diagrams from the network itself instead of from someone's memory. It collects from two kinds of source:

- **AWS:** read-only API calls across accounts and regions, including the AWS Organization itself (OUs, accounts, SCPs and other policies), collected from the management account, and with flags the Bedrock models each account can use, its custom IAM roles and users, EKS clusters, or every resource in the account.
- **Local networks:** an nmap scan for hosts, open ports and a guessed device type.

It merges the sources into one picture and draws it as **draw.io** (with the official AWS and Cisco icons), **SVG**, **Mermaid** or **Graphviz DOT**.

![An AWS account with a transit gateway, VPCs, subnets, a site-to-site VPN and Direct Connect, beside a scanned LAN of routers, switches, a server and a phone](docs/hybrid.png)

<sub>Drawn by `topomapper render` from the test fixtures (a made-up AWS account and LAN).</sub>

```
collect ─┬─ aws      boto3, read-only                 ┐
         ├─ aws-org  OUs, accounts, policies          ├─▶ topology JSON ─▶ merge ─▶ filter ─▶ render
         └─ scan     nmap host and port discovery     ┘                     diff (drift between snapshots)
```

Every stage reads and writes the same JSON, so stages chain through files or pipes. The JSON is sorted with stable IDs, so snapshots can be committed to git and compared.

## Install

```sh
pip install './tools/topomapper[aws]'    # AWS collection
pip install ./tools/topomapper           # scan, merge, diff, filter, render: no dependencies
```

Install it from this repository as shown. **`pip install topomapper` from PyPI won't work** (it isn't published there), and **`pip install topomap` installs someone else's project**: this tool was called `topomap` until that clash was found, and was renamed. Old snapshots (schema `topomap/1`) still load, and `topomapper collect <source>` still works.

| Extra | Adds | Needs |
| --- | --- | --- |
| *(none)* | `topomapper merge`, `diff`, `filter`, `render` (all formats), `anonymize` | Python 3.9+ standard library |
| `aws` | `topomapper aws`, `aws-org` | `boto3` |
| — | `topomapper scan` | the `nmap` program |

## Quick start

One command collects and draws; the extension of `-o` says what to write:

```sh
topomapper aws --profile prod -o aws.drawio                       # an AWS account, drawn
topomapper aws-org --profile management -o org.svg                # the organization chart
topomapper scan 192.168.1.0/24 -o lan.drawio                      # a local network
```

To combine sources, keep each one's topology (`.json`) and merge them. Accounts land in their OUs:

```sh
topomapper aws-org --profile management -o org.json
topomapper aws --profile prod --region us-east-1 --region eu-west-1 -o aws.json
topomapper scan 192.168.1.0/24 -o lan.json

topomapper merge org.json aws.json lan.json -o all.json -o all.drawio   # keep the result, and draw it
topomapper filter all.json --aws-only --collapse vpc -o aws-overview.svg
topomapper diff last-month.json all.json                                 # later: what changed? (exit 1 if anything did)
```

### Commands and outputs

| Command | Does |
| --- | --- |
| [`aws`](#topomapper-aws) | Collect AWS networking (VPCs, subnets, gateways, TGWs, VPN, Direct Connect), and optionally Bedrock, IAM, EKS or every resource |
| [`aws-org`](#topomapper-aws-org) | Collect an AWS Organization: OUs, accounts, policies, delegated admins |
| [`scan`](#topomapper-scan) | Discover hosts and open ports on a local network with nmap |
| [`merge`](#topomapper-merge) | Combine topologies into one |
| [`filter`](#topomapper-filter) | Cut a topology down to what you want to draw |
| [`render`](#topomapper-render) | Draw a topology saved earlier |
| [`diff`](#topomapper-diff) | Show what changed between two snapshots |
| [`anonymize`](#topomapper-anonymize) | Replace IDs, names and addresses with fakes, for sharing |

Every flag is listed under [Command reference](#command-reference); `topomapper COMMAND --help` shows the same.

- **`-o` picks the output by extension,** for every command that makes a topology: `.json` writes the topology, and `.drawio`, `.svg`, `.mmd` or `.dot` draws it. Repeat `-o` for several at once. With no `-o`, the topology JSON goes to stdout, so commands still chain with pipes (`topomapper aws ... | topomapper filter - --aws-only -o view.svg`).
- **Drawing options** (`--direction LR`, `--title`, `--no-link-labels`) work on all of them, as they do on `render`.
- **An unknown extension is refused straight away,** before a long collection starts.
- The older two-word form, `topomapper collect aws`, still works.

## Collecting

### AWS (`topomapper aws`)

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

topomapper works in the AWS GovCloud (US) partition. Credentials only work in their own partition, so a GovCloud profile is separate from a commercial one.

```sh
topomapper aws-org --profile gov-management -o topomapper-out/gov-org.json
topomapper aws --profile gov-management --region us-gov-west-1 --region us-gov-east-1 \
    --iam -o topomapper-out/gov.json
```

- **Home region:** with no `--region` and no region in the profile, topomapper tries the commercial, GovCloud and China starting regions in turn, so the partition is found from the credentials. Give `--region us-gov-west-1` to skip the probing.
- **Regions:** `describe-regions` lists `us-gov-east-1` and `us-gov-west-1`. If that listing is denied, the fallback is `us-gov-west-1`.
- **Service lookups:** whether Bedrock or EKS exists in a region is checked in that region's own partition. GovCloud has both in `us-gov-west-1` and `us-gov-east-1`.
- **ARNs:** `--role-arn` and `--org-role-name` use the partition the credentials belong to (`arn:aws-us-gov:...`).
- **Merging:** account IDs in GovCloud and commercial are different accounts, so merging both into one diagram is safe. Nothing links the two partitions.
- **Tested with an emulator only.** The GovCloud code paths are exercised against moto in `us-gov-west-1` and with stubbed partition lookups, never against a real GovCloud account. Please report anything that behaves differently there. Bedrock availability in GovCloud follows the same API but I haven't seen real GovCloud answers.

### Bedrock, IAM and EKS (`--ai`, `--iam`, `--eks`)

Three flags on `topomapper aws` add what the network collection doesn't cover. Each is off by default, read-only, and works across every account `topomapper aws` reaches (`--org-role-name` for the whole organization).

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
topomapper aws-org --profile management -o topomapper-out/org.json
topomapper aws --profile management --org-role-name OrganizationAccountAccessRole \
    --ai --skip-network -o topomapper-out/ai.json
topomapper merge topomapper-out/org.json topomapper-out/ai.json -o topomapper-out/org-ai.json
topomapper filter topomapper-out/org-ai.json --kind bedrock-model --hide-empty -o topomapper-out/models.json
topomapper render topomapper-out/models.json -o topomapper-out/models.svg
```

`filter --kind bedrock-model` keeps the models and everything above them (region, account, OU, organization). The same works for `--kind iam-role --kind iam-user` (an IAM view with trust links) and `--kind eks-cluster`. Roles, users and cluster names identify an estate, so `topomapper anonymize` replaces them (and `--strict` drops everything else but a short list of facts).

### Every resource in an account (`--all-resources`)

`topomapper aws --all-resources` draws everything in the account, not only the network: every Lambda function, bucket, queue, table, key, role, log group and the rest, each with its service's AWS icon.

```sh
topomapper aws --profile prod --all-resources -o topomapper-out/everything.json
topomapper filter topomapper-out/everything.json --max-per-type 10 | topomapper render - -o topomapper-out/everything.drawio
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

### AWS Organizations (`topomapper aws-org`)

![An organization chart: the root with its SCP, the management account, and Infrastructure, Sandbox, Security and Workloads OUs (Workloads holding Prod and NonProd), each OU listing its SCPs and accounts](docs/organization.png)

<sub>Drawn from a mocked organization.</sub>

- **Run it against the management account,** or an account that is a delegated administrator for AWS Organizations. Only those can list the whole organization. A member account gets a clear error saying so.
- **Permissions:** the AWS-managed `AWSOrganizationsReadOnlyAccess` policy is enough. Like `topomapper aws`, it can only call `Describe*`/`List*`/`Get*`, plus `AssumeRole` with `--role-arn`.
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
- **Combining with network data:** `topomapper aws` gives each account the same ID, so `merge org.json aws.json` draws each account inside its OU, with its regions and VPCs inside the account. After a merge, an account that `topomapper aws` could not reach is labelled "not collected", an empty OU "empty" and a region with nothing in it "no resources", so a bare box is never mistaken for an unscanned one. An account with nothing collected inside it is drawn as an account icon; one holding VPCs is drawn as a container.

### Home and lab networks (`topomapper scan`)

Finds the hosts and open ports on a network with [nmap](https://nmap.org) and draws them: one segment per network, every host linked to it, the default gateway as the router, and each host's services and a guessed type (gateway, server, NAS, printer, camera, access point, media player…) on its label.

```sh
topomapper scan 192.168.1.0/24 --save-xml topomapper-out/scan -o topomapper-out/lan.json
topomapper render topomapper-out/lan.json -o topomapper-out/lan.svg
```

- **Two nmap runs:** a host sweep (`-sn`), then `--top-ports 1000` with service detection (`-sV`) on the hosts that answered. `--ports 22,80,1-1024`, `--no-ports`, `--no-service-detection`, `--full-probes` (every service probe: exact but slow on routers), `--no-dns`, `--timing 0-5`, `--exclude ADDR` and `--os` (needs root) change that.
- **Root or not:** with `sudo`, nmap finds hosts by ARP and reads MAC addresses itself. Without it, hosts are found with TCP pings plus this machine's ARP cache after the sweep (`--no-arp-cache` turns that off), and vendors come from nmap's own MAC prefix list.
- **Safety:** only private, link-local and CGNAT (Tailscale) space is scanned unless you add `--allow-public`, and at most 65,536 addresses per run.
- **Offline:** `--from-xml FILE` (repeatable) redraws saved `nmap -oX` output without scanning.
- Needs the `nmap` program (`brew install nmap`, `apt install nmap`, `pacman -S nmap`), not a Python package.

## Combining, trimming and comparing

- **`merge`** unions topologies: accounts land in their OUs, and anything collected twice becomes one node. Nodes that disagree about their kind or parent are listed in a warning.
- **`filter`** shrinks big estates down to a readable picture. It takes `--aws-only` / `--network-only`, `--kind`, `--exclude-kind az` (children move up), `--collapse vpc` (subnets fold into their VPC, and links are re-pointed), `--max-per-type N` (more than N resources of one type in a box become one counted node), `--around tgw-01 --depth 2`, `--match REGEX` and `--hide-empty` (drops regions and OUs in which nothing was found).
- **`diff`** shows what was added, removed and changed, down to the attribute (`tags.Owner: net → platform`). It writes text or JSON, and exits 1 when anything changed, so it can watch for drift in CI.

## Drawing (`render`)

| Format | Notes |
| --- | --- |
| `drawio` | Opens in draw.io / diagrams.net and can be exported to Visio. Uses draw.io's built-in AWS 2021 and Cisco 19 shapes, with nested containers (account › region › VPC › AZ › subnet). Written as uncompressed XML, so it diffs cleanly and can be redacted as text. |
| `svg` | Standalone, uses the same layout and needs no Graphviz. |
| `mermaid` | Paste into GitHub or GitLab Markdown, Notion, etc. |
| `dot` | For Graphviz (`dot -Tpng`). |

- **Layout:** read top to bottom: internet, then edge gateways and firewalls, then routers, core switches, access switches and endpoints.
- **Output is deterministic:** the same input gives byte-identical output.
- **Options:** `--direction LR` reads left to right, `--title` adds a title, and `--no-link-labels` drops port and subnet labels.

### Sharing and committing output safely

Snapshots and diagrams carry account IDs, names, tags and addresses from the real estate. Two things help:

- **Keep real output out of git.** Write it under `topomapper-out/`, which the repo ignores (`-o topomapper-out/all.json`; the folder is created for you). Real output is written readable by you only (mode 0600). If it lands inside a git repository that doesn't ignore it, topomapper prints a note. That covers `-o`, a redirected stdout (`> out.json`) and a `scan --save-xml` folder.
- **`anonymize` makes a shareable copy.** Render from the anonymized JSON and the diagram is safe too:

```sh
topomapper anonymize topomapper-out/all.json -o sample.json
topomapper render sample.json -o sample.svg
```

  It replaces account, organization, OU and resource IDs (same prefix and length), names and tag values (`account-1`, `ou-2`), descriptions, serial numbers, source file names, e-mail addresses and MAC addresses. The same real value always gets the same fake, and parent/child structure is kept. Nothing maps back, because no table or key is written. If any original ID is still in the output, there's a warning.
  - **Addresses:** `--ips ipredact` (the default) runs the addresses through [`tools/ip-redactor`](../ip-redactor), keeping subnets and containment true. Install it first (`pip install ./tools/ip-redactor`); without it, the command stops with that instruction. `--ips public` needs nothing: it moves only public addresses into the documentation ranges and leaves private ones as they are. `--ips keep` leaves all of them. `--ip-key-file` gives the same addresses on every run.
  - **`--keep-names`** leaves names and tag values alone; IDs and addresses are still replaced.
  - **`--strict`** also drops every attribute that isn't on the list of known, non-free-text ones (tags, descriptions, serials, anything unfamiliar). Use it when you don't know what a field might contain.
  - **Checks:** the output is searched for the original IDs, names and public addresses, and anything that survived is a warning. Fakes never equal a real value, and link order stays canonical, so `diff` and `merge` work on anonymized snapshots.

The text outputs (draw.io, SVG, Mermaid, DOT) can also be run through `ipredact` directly, but that doesn't touch IDs or names.

## Command reference

Every command prints this with `topomapper COMMAND --help`. Flags marked *repeatable* can be given several times.

### Options shared by every command that makes a topology

`aws`, `aws-org`, `scan`, `merge`, `filter` and `anonymize` all take these:

| Flag | What it does |
| --- | --- |
| `-o, --output FILE` | Where to write, chosen by extension: `.json` writes the topology; `.drawio`, `.svg`, `.mmd`/`.mermaid` or `.dot`/`.gv` draws it. Repeatable, to write several at once. Default: the topology JSON on stdout. Any other extension is refused before anything runs. |
| `--direction {TB,LR}` | Diagram reading order: top-to-bottom (default) or left-to-right. |
| `--title TITLE` | Title drawn above the diagram. |
| `--no-link-labels` | Leave ports, subnets and VPN/VIF IDs off the links. |

The drawing options only matter when an `-o` names a diagram. Files are written readable by you only (mode 0600), except from `anonymize`.

### `topomapper aws`

Collect VPCs, subnets, gateways, transit gateways, VPNs, peering and Direct Connect from one or more AWS accounts, read-only. Details: [AWS](#aws-topomapper-aws).

```
topomapper aws [--profile NAME] [--region NAME] [--role-arn ARN | --org-role-name NAME]
            [--ai] [--iam] [--eks] [--all-resources] [--skip-network] [--include LIST] [-o FILE]
```

| Flag | What it does |
| --- | --- |
| `--profile NAME` | AWS profile to use. Repeatable, for several accounts. Default: boto3's credential chain. |
| `--region NAME` | Region to collect. Repeatable. Default: every region enabled for the account. |
| `--role-arn ARN` | Assume this role from each profile and collect its account. Repeatable. The profiles' own accounts are then left out unless `--org-role-name` covers them. |
| `--external-id ID` | External ID to pass when assuming roles. |
| `--role-session-name NAME` | Session name for assumed roles (default: `topomapper`). |
| `--org-role-name NAME` | List the organization's accounts and assume `arn:aws:iam::<id>:role/NAME` in each; the caller's own account is collected directly. |
| `--include LIST` | Comma-separated extras: `instances` (EC2 instances), `endpoints` (VPC endpoints), `tags` (keep resource tags), `relationships` (with `--all-resources`, draw AWS Config's relationships between resources). |
| `--ai` | Also collect the Bedrock models each account uses or is granted, per region. See [Bedrock, IAM and EKS](#bedrock-iam-and-eks---ai---iam---eks). |
| `--iam` | Also collect custom IAM roles and users: trust, policy names, MFA and access-key age. No credentials or policy documents are read. |
| `--iam-all` | With `--iam`, include service-linked and IAM Identity Center roles too. |
| `--eks` | Also collect EKS clusters: version, endpoint access, node groups, Fargate profiles and add-ons. |
| `--all-resources` | Also collect every resource in the account, from AWS Config and Resource Explorer, placed in its VPC or subnet when known. See [Every resource](#every-resource-in-an-account---all-resources). |
| `--skip-network` | Skip the VPC, gateway, TGW, VPN and Direct Connect collection: a faster run when you only want `--ai`, `--iam`, `--eks` or `--all-resources`. |
| `--workers N` | Account-and-region pairs collected in parallel (default: 8). |

### `topomapper aws-org`

Collect an AWS Organization from its management account (or a delegated administrator), read-only. Details: [AWS Organizations](#aws-organizations-topomapper-aws-org).

```
topomapper aws-org [--profile NAME] [--role-arn ARN] [--policies {attrs,nodes,none}] [--include LIST] [-o FILE]
```

| Flag | What it does |
| --- | --- |
| `--profile NAME` | AWS profile for the management account. Default: boto3's credential chain. |
| `--role-arn ARN` | Assume this role in the management account first. |
| `--external-id ID` | External ID to pass when assuming the role. |
| `--role-session-name NAME` | Session name for the assumed role (default: `topomapper`). |
| `--policies {attrs,nodes,none}` | How to show organization policies: `attrs` writes them on the root, OUs and accounts they're attached to (default); `nodes` also draws each policy as a box linked to its targets; `none` leaves them out. |
| `--include LIST` | Comma-separated extras: `tags` (account tags, one call per account), `emails` (account email addresses, personal data, so off by default), `aws-managed-policies` (`FullAWSAccess` and the like, attached everywhere so hidden by default). |

### `topomapper scan`

Discover hosts and open ports on a local network with nmap, and draw them. Details: [Home and lab networks](#home-and-lab-networks-topomapper-scan).

```
topomapper scan TARGET [TARGET ...] [--exclude ADDR] [--top-ports N | --ports SPEC | --no-ports] [--os] [-o FILE]
topomapper scan --from-xml FILE [TARGET ...] [-o FILE]
```

| Flag | What it does |
| --- | --- |
| `TARGET` | Networks or addresses to scan, e.g. `192.168.1.0/24`. With `--from-xml`, they only say which networks to draw. |
| `--exclude ADDR` | Address or network not to scan. Repeatable. |
| `--allow-public` | Allow targets outside private, link-local and CGNAT space. |
| `--from-xml FILE` | Read saved `nmap -oX` output instead of scanning. Repeatable. |
| `--top-ports N` | Scan nmap's N most common TCP ports (default: 1000). |
| `--ports SPEC` | Ports to scan, in nmap syntax: `22,80,1-1024`. |
| `--no-ports` | Only discover hosts. |
| `--no-service-detection` | Don't probe open ports for service names and versions (`-sV`); faster. |
| `--full-probes` | Service detection tries every probe, not nmap's light set: more exact versions, but minutes per host on slow devices such as routers. |
| `--os` | Guess operating systems (`-O`; needs root). |
| `--no-dns` | Don't look up host names. |
| `--no-arp-cache` | Without root, don't add hosts from this machine's ARP cache that the TCP ping sweep missed. |
| `--timing 0-5` | nmap timing template, slower to faster (default: 4). |
| `--gateway ADDR` | The network's gateway. Default: this machine's default gateway. |
| `--nmap PATH` | nmap program to run (default: `nmap`). |
| `--save-xml DIR` | Also save nmap's XML output to DIR (mode 0600), for `--from-xml`. |

### `topomapper merge`

Combine topologies into one: accounts land in their OUs, and anything collected twice becomes one node. Details: [Combining](#combining-trimming-and-comparing).

```
topomapper merge INPUT [INPUT ...] [-o FILE]
```

| Flag | What it does |
| --- | --- |
| `INPUT` | Topology JSON files to merge; `-` reads stdin (once). |

### `topomapper filter`

Cut a topology down to the part you want to draw. Every selecting flag must match for a node to stay; the containers above a kept node stay too. Details: [Combining](#combining-trimming-and-comparing).

```
topomapper filter [INPUT] [--kind KIND] [--exclude-kind KIND] [--collapse KIND] [--around NODE --depth N]
               [--match REGEX] [--aws-only | --network-only] [--hide-empty] [--max-per-type N] [-o FILE]
```

| Flag | What it does |
| --- | --- |
| `INPUT` | Topology JSON to filter (default: stdin). |
| `--kind KIND` | Keep only nodes of this kind, plus their ancestors. Repeatable. |
| `--exclude-kind KIND` | Remove nodes of this kind; a removed container's children move up to its parent. Repeatable. |
| `--collapse KIND` | Fold everything inside each node of this kind into it, re-pointing links (e.g. `--collapse vpc`). Repeatable. |
| `--max-per-type N` | Where a box holds more than N resources of one type (from `topomapper aws --all-resources`), draw them as one node that counts them. |
| `--around NODE` | Keep only what is within `--depth` link hops of this node (ID or exact label) or of its contents. |
| `--depth N` | Link hops for `--around` (default: 1). |
| `--match REGEX` | Keep only nodes whose label or ID matches; prefix `(?i)` to ignore case. |
| `--hide-empty` | Remove regions and OUs flagged empty (nothing found in them). |
| `--aws-only` | Keep only AWS nodes, and external nodes linked to them. |
| `--network-only` | Keep only network nodes (from `scan`), and external nodes linked to them. |

### `topomapper render`

Draw a topology saved earlier. Commands that make a topology can draw directly with `-o`, so this is for JSON you already have. Details: [Drawing](#drawing-render).

```
topomapper render INPUT [-o FILE] [-f FORMAT] [--direction {TB,LR}] [--title TITLE] [--no-link-labels]
```

| Flag | What it does |
| --- | --- |
| `INPUT` | Topology JSON file, or `-` for stdin. |
| `-o, --output FILE` | Where to write the diagram (default: stdout). |
| `-f, --format {drawio,svg,mermaid,dot}` | Output format. Default: from the `-o` extension, else draw.io. |
| `--direction {TB,LR}` | Top-to-bottom (default) or left-to-right reading order. |
| `--title TITLE` | Title drawn above the diagram. |
| `--no-link-labels` | Leave ports, subnets and VPN/VIF IDs off the links. |

### `topomapper diff`

Show what was added, removed and changed between two snapshots, down to the attribute. Exits 1 when anything changed, so it can watch for drift in CI.

```
topomapper diff OLD NEW [--format {text,json}] [--ignore-attr NAME] [--summary]
```

| Flag | What it does |
| --- | --- |
| `OLD`, `NEW` | The earlier and later snapshots; `-` for stdin. |
| `--format {text,json}` | Output format (default: text). |
| `--ignore-attr NAME` | Ignore this attribute: a key at any depth (`state`) or a dotted path (`tags.LastSeen`). Repeatable. `sources` and meta are always ignored. |
| `--summary` | Print counts only. |

### `topomapper anonymize`

Replace account IDs, resource IDs, names, tags and addresses with consistent fakes, so a snapshot (and the diagrams drawn from it) can be shared or committed. Details: [Sharing and committing output safely](#sharing-and-committing-output-safely).

```
topomapper anonymize [INPUT] [--ips {ipredact,public,keep}] [--ip-key-file FILE] [--strict] [--keep-names] [-o FILE]
```

| Flag | What it does |
| --- | --- |
| `INPUT` | Topology JSON to anonymize (default: stdin). |
| `--ips {ipredact,public,keep}` | Addresses: `ipredact` keeps subnets and containment true but needs the `ipredact` tool (default); `public` moves only public addresses into the documentation ranges; `keep` leaves them. |
| `--ip-key-file FILE` | `ipredact` key file, to get the same fake addresses on every run. |
| `--strict` | Also drop every attribute that isn't on the known, non-free-text list (tags, descriptions, serials, anything unfamiliar). |
| `--keep-names` | Leave names and tag values alone; IDs and addresses are still replaced. |

## Limits

- **Lines are straight.** In dense areas they can cross icons and other containers, and port labels can overlap. Tidy up in draw.io if it matters.
- **AWS resources not collected:** DX LAGs, TGW Connect peers and Cloud WAN; load balancers, security groups and everything else only with `--all-resources`. From Organizations: policy contents (only names and attachments), and SCP effects on accounts.
- **Bedrock, IAM and EKS tests use emulators and stubs.** moto has no Bedrock API, so `--ai` is tested with stubbed answers; IAM and EKS run against moto. All three have also been run against a real lab organization, but only that one, so please report anything that looks wrong.
- **Trust links are drawn at collection time,** between accounts collected in the same run. Merging snapshots from separate runs doesn't add links between them. A role that trusts an account outside the run keeps it in `trusted_accounts` but gets no link.
- **IAM scope:** policy documents, permission analysis and credentials are never read, so "admin" means the AdministratorAccess managed policy is attached, not that the effective permissions were computed.
- **`--all-resources` is tested with stubbed Config, Resource Explorer and Cloud Control answers** (the emulator has none of them), not yet against a real account. Please report resources that land in the wrong place.

## Development

```sh
pip install './tools/topomapper[test]'
cd tools/topomapper && python3 -m unittest discover -s tests
```

[DESIGN.md](DESIGN.md) is the contract between the stages: IDs, kinds, attributes and containment. `tests/test_e2e.py` runs every stage through the CLI, from mocked AWS plus saved nmap output in `tests/fixtures/scan/` to every output format.
