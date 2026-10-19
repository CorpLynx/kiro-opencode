# ipam-infoblox: AWS IPAM export to Infoblox import

`ipam-infoblox` turns an AWS IPAM export (CSV or Excel) into a file Infoblox's CSV import accepts, and checks it against the Grid first, read-only:

- **Cleans the export:** finds the header row under a title, reads the columns whatever they are called, splits cells holding several CIDRs, clears host bits (`10.1.1.7/24` → `10.1.1.0/24`), and leaves out Elastic IPs and rows without a CIDR. Every change and every row left out is listed with its row number.
- **Chooses container or network:** IPAM pools and VPCs become network containers, subnets become networks, and anything that holds other ranges is promoted to a container. Containers come first in the file, widest first.
- **Checks the Grid (read-only):** with `--wapi`, ranges already in Infoblox are left out. So are the ones Infoblox would reject: a range inside an existing *network* (a network can't hold networks) and overlapping VPCs in one network view. Missing network views and extensible attributes are reported too, as are values a list-type attribute doesn't allow.
- **Writes a report** for the change review: what to fix, what goes in and where it lands, what's already there, and what was changed or left out.

It never writes to Infoblox. The import itself stays in Grid Manager, where it's tested, reviewed and logged.

```
export.xlsx ─▶ read + clean ─▶ plan (container/network, merge, nest) ─▶ compare with Grid (GET only) ─▶ import.csv + report.md
```

## Install

```sh
pip install './tools/ipam-infoblox[xlsx]'   # reads .xlsx
pip install ./tools/ipam-infoblox           # CSV only: no dependencies
```

| Extra | Adds | Needs |
| --- | --- | --- |
| *(none)* | everything, for CSV exports | Python 3.9+ standard library |
| `xlsx` | reading `.xlsx` / `.xlsm` | `openpyxl` |

## Use

```sh
# 1. How is the export read? Which columns, how many ranges, what gets fixed?
ipam-infoblox inspect ipam-export.xlsx

# 2. Write the import, checked against the Grid
export INFOBLOX_USERNAME=ipam-reader            # the password comes from $INFOBLOX_PASSWORD or a prompt
ipam-infoblox plan ipam-export.xlsx --wapi https://gm.example.net \
    --ea "AWS Account=account" --ea "AWS Region=region" \
    -o import.csv --report report.md
```

Exit status is `0` when the import is clean, `1` when the report lists something to fix first, and `2` on errors, so it can gate a pipeline. The import file is still written with exit status `1`, holding only the ranges that can go in.

The sample export in [`tests/fixtures/ipam-export.csv`](tests/fixtures/ipam-export.csv) shows the shape it expects; `ipam-infoblox inspect` on it is a quick way to see what each step does.

### Reading the export

- **Formats:** `.csv` (comma, semicolon, tab or pipe separated; UTF-8, or the Windows-1252 that Excel on Windows saves, which `inspect` and the report then mention), `.xlsx` and `.xlsm`. Save old `.xls` files as `.xlsx` first. In a workbook, the first sheet with a CIDR column is used, or the one named with `--sheet`.
- **Columns** are recognized by name, in the forms the AWS console, the API (`ResourceCidr`, `ResourceOwnerId`) and hand-kept sheets use. `inspect` shows the mapping. Override any of them with `--column FIELD=HEADER`; `--column cidr=...` also tells it which row is the header when the CIDR column has an unusual name:

  | Field | Recognized headers (case and spacing don't matter) |
  | --- | --- |
  | `cidr` (required) | Resource CIDR, CIDR, CIDR block, Prefix, Network, ... |
  | `resource_type` | Resource type, Type |
  | `resource_id` | Resource ID, ID |
  | `name` | Name, Resource name, Name tag |
  | `region` | Resource region, Region, Locale |
  | `account` | Resource owner ID, Resource owner, Owner ID, Account ID |
  | `vpc_id`, `pool_id`, `description` | VPC ID; IPAM pool ID, Pool ID; Description |

- **Types:** `vpc`, `subnet`, `pool` (any IPAM pool), `public-ipv4-pool`, `ipv6-pool` and `allocation` are understood however they're capitalized or spaced. `eip` and `eni` are addresses, not networks, and are left out unless you add `--keep-type eip`. `--drop-type pool` leaves out a type.

### What goes in Infoblox

- **Network view:** `--network-view` (default `default`) can use fields, so `--network-view 'aws-{account}'` gives each AWS account its own view. Do that when accounts reuse the same ranges (two VPCs on `10.0.0.0/16` in one view is a conflict). A row missing a field the view needs (a pool with no owner, say) is left out and listed, rather than landing in a view called `aws-`.
- **Comment:** `--comment` (default `{name} ({resource_id})`) takes the same fields, plus `{column:HEADER}` for any other column. Blank fields are tidied away and comments are cut to Infoblox's 256 characters.
- **Extensible attributes:** `--ea NAME=SOURCE` (repeatable) sets attribute `NAME` from a field (`account`, `region`, `resource_id`, `resource_type`, `name`, `vpc_id`, `pool_id`, `cidr`, ...), from `column:HEADER`, or to fixed text with `text:VALUE`. The attributes must already be defined in the Grid; with `--wapi` that's checked, along with the allowed values of list-type attributes.
- **Merged ranges:** the same CIDR twice in one view (an IPAM pool and the VPC allocated from it) is imported once, described by the most specific of its rows. When a subnet covers its whole VPC, the range is imported as a network, so it can hold addresses.

### The import file

```
header-networkcontainer,address*,netmask*,network_view,comment,EA-AWS Account
networkcontainer,10.0.0.0,16,default,prod (vpc-0a1b2c3d4e5f60001),111122223333
header-network,address*,netmask*,network_view,comment,EA-AWS Account
network,10.0.1.0,255.255.255.0,default,prod-app-a (subnet-0a1b2c3d4e5f60001),111122223333
header-ipv6network,address*,cidr*,network_view,comment,EA-AWS Account
ipv6network,2001:db8:1000:1::,64,default,v6-a (subnet-0a1b2c3d4e5f60005),111122223333
```

- **Netmasks:** IPv4 networks get a dotted netmask and IPv4 containers a prefix length, as the NIOS CSV Import Reference documents them. `--netmask-style dotted` or `prefix` forces one form for both if your NIOS version wants otherwise. IPv6 always uses a prefix length in `cidr*`.
- **`--split`** writes `import-containers.csv` and `import-networks.csv`, for importing in two steps. The NIOS 8.6 guide says IPv6 network containers can't be CSV-imported; if your version refuses them, create those containers in Grid Manager and import the rest.
- **Importing:** use Grid Manager's CSV Import with the **Add** action, and run its test first. If this is your first import on a NIOS version, try a copy with a few rows.

### Checking against Infoblox

- **What is read:** network views, IPv4 and IPv6 networks and network containers in the views the import uses, and extensible attribute definitions. That's all, through WAPI `GET` requests; the client has no other method. A read-only Grid user with permission to see those networks is enough.
- **Connection:** `--wapi https://gm.example.net` (WAPI version `2.12` unless `--wapi-version` says otherwise). The certificate is verified; pass your CA with `--ca-bundle`, or `--insecure` in a lab. `http://` URLs are refused unless `--allow-http`, because they would send the password in clear, and redirects are refused, because following one would send the password wherever it points. The standard `HTTPS_PROXY` and `NO_PROXY` variables apply.
- **Credentials:** the user from `--username` or `$INFOBLOX_USERNAME`, the password from `$INFOBLOX_PASSWORD` or a prompt, never from the command line. One login is reused for the whole run.
- **Results for each range:**
  - *new:* goes in the import file;
  - *already in Infoblox:* the same CIDR in the same view, left out, with any difference noted (it's a container there but a network here, say);
  - *conflict:* left out and explained. It sits inside an existing network, or it's a network in Infoblox that the export puts other ranges in, or two VPCs or subnets claim it in one view, or its network view doesn't exist in the Grid.
- A range that holds networks already in Infoblox is imported as a container, so they end up inside it.

## Handling the output

The export, the import file and the report describe a real address plan. Output files are written readable by you only (mode 0600). Keep them out of git; this repository's own checks reject real addresses. To share an example, run it through [`tools/ip-redactor`](../ip-redactor) first.

## Limits

- **Tested against a fake Grid**, a local WAPI server in the tests that follows the documented paging and errors, not against a real NIOS appliance. The import format follows the NIOS CSV Import Reference; please report anything your NIOS version rejects.
- **The AWS console's export columns aren't documented,** so the column names come from the IPAM API and common spellings. If a column isn't picked up, `--column` fixes it; please report the header so it can be added.
- **Not handled:** DHCP ranges, fixed addresses and DNS records; attributes on ranges that already exist aren't updated (they're left out, not merged); and Infoblox's mandatory-attribute settings aren't read, so a Grid that requires an attribute on every network needs it passed with `--ea`.

## Development

```sh
pip install './tools/ipam-infoblox[test]'
cd tools/ipam-infoblox && python3 -m unittest discover -s tests
```

`tests/fake_wapi.py` is the fake Grid Manager: a real HTTP server on localhost speaking enough WAPI (paging, Basic auth, the `ibapauth` cookie, errors) to test the client end to end. It records every request, and the tests check that none but `GET` was sent.
