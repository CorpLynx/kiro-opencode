# ipredact — structure-preserving IP redaction

`x.x.1.1`-style redaction throws away structure: you can no longer tell which
hosts share a subnet or which route covers which network. `ipredact` instead
**rewrites the first two octets with different numbers** and keeps the last two,
so the redacted output is unusable for reaching real infrastructure but still
reads like a real network.

```
ip address 10.42.7.1 255.255.255.0          ->  ip address 253.245.7.1 255.255.255.0
ip route 10.0.0.0 255.0.0.0 172.16.4.1      ->  ip route 253.0.0.0 255.0.0.0 53.149.4.1
vpc cidr 10.42.0.0/16 subnet 10.42.7.0/24   ->  vpc cidr 253.245.0.0/16 subnet 253.245.7.0/24
host ip-10-42-7-15.ec2.internal             ->  host ip-253-245-7-15.ec2.internal
```

## What it guarantees

- **Consistent.** The same address always maps to the same output for a given key, across lines, files and runs.
- **No collisions.** Two different addresses never redact to the same one.
- **Prefix-preserving.** Addresses that share their first N bits still share them after redaction, so `/8`, `/12`, `/16` and `/24` containment all hold. `10.0.0.0/8` still contains every redacted `10.x.x.x`.
- **Always different.** Both the first and the second octet change for every address.
- **Reversible with the key.** `--reverse` turns redacted output back into the original addresses.

It works by applying a keyed bit-flip to each of the top 16 bits, where each flip depends on the original bits above it. This is the idea behind Crypto-PAn, limited to two octets.

## Usage

Requires Python 3.8+ and nothing else. `.xlsx` files also need `openpyxl`.

```sh
# stdin -> stdout, random key printed to stderr
python3 ipredact.py < running-config.txt

# fixed key so several runs/files stay consistent
python3 ipredact.py -k "$SECRET" router1.cfg -o router1.redacted.cfg
IPREDACT_KEY="$SECRET" python3 ipredact.py *.log -d redacted/

# spreadsheets (every text cell is redacted, other cells untouched)
python3 ipredact.py ipam-export.xlsx -o ipam-export.redacted.xlsx -k "$SECRET"

# keep the key and a lookup table (both sensitive: keep them private)
python3 ipredact.py dump.txt -d out/ --save-key redact.key --map-out mapping.csv

# undo
python3 ipredact.py -r --key-file redact.key < out/dump.txt
```

| Option | Purpose |
| --- | --- |
| `-o FILE` / `-d DIR` / `-i` | Write to a file, into a directory, or in place. The default is stdout. |
| `-k KEY`, `--key-file F`, `$IPREDACT_KEY` | Mapping key. If none is given, a random key is generated. |
| `--save-key F` | Save the key that was used. |
| `-r, --reverse` | Restore the original addresses. Needs the key. |
| `-x CIDR` | Leave a network untouched. Repeatable. |
| `--no-default-excludes` | Also redact `0/8`, `127/8`, `169.254/16` and `255/8`. |
| `--no-dashed` | Skip `ip-10-1-2-3` style hostnames. |
| `--map-out CSV` | Write a table of every original and redacted address. |

## Detection notes

- It finds IPv4 addresses, `addr/len`, and `addr mask` or `addr wildcard` pairs (Cisco `network ... mask ...`, OSPF wildcards, `ip route`). It also finds AWS `ip-a-b-c-d` hostnames.
- When a network address is written with a prefix shorter than /16, either as `/len` or as a dotted mask, the output is re-aligned so it is still a valid network address. Interface addresses such as `10.5.0.1/8` are not changed this way.
- Netmasks, wildcard masks, loopback, link-local (including the `169.254.169.254` metadata address) and `0.0.0.0` are left alone by default.
- Version strings and OIDs such as `1.2.3.4.5` and `1.3.6.1.4.1.9` are not touched.
- IPv6 and `in-addr.arpa` names are not handled yet.
- Redacted addresses can fall anywhere in IPv4 space, including someone else's public range. That is the trade-off for keeping structure. In rare cases an output lands in an excluded range; if it does, the tool prints a warning, because `--reverse` will leave those addresses unchanged.

## Tests

```sh
python3 -m unittest test_ipredact -v
```
