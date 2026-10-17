# ipredact: structure-preserving IP redaction

Redacting addresses as `x.x.1.1` throws away structure. Afterwards you can't tell which hosts share a subnet or which route covers which network. `ipredact` instead **rewrites the network part of every address with different numbers** and keeps the host part:

- **IPv4:** the first two octets are rewritten and the last two are kept.
- **IPv6:** the routing prefix (the first 48 bits by default, the /48 site prefix) is rewritten. The subnet ID and interface ID are kept.

The output can't be used to reach real infrastructure, but it still reads like a real network:

```
ip address 10.42.7.1 255.255.255.0             ->  ip address 68.232.7.1 255.255.255.0
ip route 10.0.0.0 255.0.0.0 172.16.4.1         ->  ip route 68.0.0.0 255.0.0.0 160.146.4.1
vpc 10.42.0.0/16 subnet 10.42.7.0/24           ->  vpc 68.232.0.0/16 subnet 68.232.7.0/24
ipv6 address 2001:db8:abcd:12::1/64            ->  ipv6 address 392a:9c05:adc0:12::1/64
ipv6 route 2001:db8:abcd::/48                  ->  ipv6 route 392a:9c05:adc0::/48
ula fd12:3456:789a:1::10                       ->  ula fd05:2fb1:14bf:1::10
mapped ::ffff:10.42.7.15                       ->  mapped ::ffff:68.232.7.15
host ip-10-42-7-15.ec2.internal                ->  host ip-68-232-7-15.ec2.internal
15.7.42.10.in-addr.arpa.                       ->  15.7.232.68.in-addr.arpa.
padded 010.042.007.001                         ->  padded 068.232.007.001
mac 00:1a:2b:3c:4d:5e  time 12:34:56.789       ->  (untouched)
lo 127.0.0.1 ::1  md 169.254.169.254 fe80::1   ->  (untouched)
```

## Guarantees

- **Consistent.** With a given key, the same address always becomes the same output, across lines, files and runs. That holds across forms too: `10.42.7.15`, `::ffff:10.42.7.15`, `ip-10-42-7-15` and its PTR name all agree.
- **No collisions.** Two different addresses never redact to the same one.
- **Prefix-preserving.** Addresses that share a prefix still share it after redaction. For IPv4 that holds at /8 and longer; for IPv6 at any length. CIDR containment, summaries and route coverage stay true.
- **Always different.** For IPv4, the first and the second octet both change for every address. For IPv6, every rewritten hextet changes.
- **Output stays plausible.**
  - IPv4 output never lands in `0/8`, `127/8`, `255/8` or `169.254/16`.
  - IPv6 global unicast (`2000::/3`) stays global unicast, and ULA (`fc00::/7`) stays ULA.
  - Redacted addresses never become link-local or multicast.
- **Reversible with the key.** `--reverse` restores the originals.

### How it works

- **IPv4 first octet:** a keyed shuffle of 1–254 (without 127) that leaves no value unchanged.
- **IPv4 second octet and the IPv6 prefix:** each bit is flipped by a keyed HMAC-SHA256 function of the original bits above it. This is the Crypto-PAn idea, limited to the network bits.
- **Key secrecy:** without the key, you can't work out the mapping from the output.

**One exception per key:** if one /16 happens to map onto `169.254/16`, it is moved to the slot `169.254` would have had. That /16 is still unique and reversible, but its /8 grouping is not preserved.

## Usage

Requires Python 3.8+ and only the standard library. `.xlsx` and `.xlsm` files also need `openpyxl`.

```sh
# stdin -> stdout, random key printed to stderr
python3 ipredact.py < running-config.txt

# keep the key in a file so every run/file uses the same mapping
python3 ipredact.py configs/ -d redacted/ --save-key redact.key
python3 ipredact.py more-logs/*.log -d redacted/ --key-file redact.key

# spreadsheets: cell text, comments, hyperlinks and sheet names
python3 ipredact.py ipam-export.xlsx -o ipam-export.redacted.xlsx --key-file redact.key

# a lookup table of every real -> redacted address
python3 ipredact.py dump.txt -o dump.red.txt --key-file redact.key --map-out mapping.csv

# undo (same key, same --v6-bits and exclude options)
python3 ipredact.py -r --key-file redact.key < dump.red.txt
```

| Option | Purpose |
| --- | --- |
| `-o FILE` / `-d DIR` / `-i` | Write to a file, into a directory (mirroring the layout of directory inputs), or in place. The default is stdout. |
| `--key-file F`, `$IPREDACT_KEY`, `-k KEY` | Mapping key. `-k` shows up in `ps` and shell history, so prefer the other two. If none is given, a random 128-bit key is generated. |
| `--save-key F` | Save the key that was used (written with mode 600). |
| `-r, --reverse` | Restore the original addresses. |
| `-x CIDR` | Leave an IPv4 or IPv6 network untouched. Repeatable. |
| `--no-default-excludes` | Also redact `0/8`, `127/8`, `169.254/16`, `255/8`, `::/127`, `fe80::/10` and `ff00::/8`. |
| `--v6-bits N` | Rewrite the first 16, 32, 48 (default) or 64 IPv6 bits. |
| `--no-dashed` | Skip `ip-10-1-2-3` style hostnames. |
| `--map-out CSV` | Write a table of every original and redacted address (mode 600). |
| `-v` | Print a summary of counts to stderr. |

Exit status is 0 on success and 1 if any input was skipped (for example a binary file or an unreadable workbook). Skipped files are named on stderr and the remaining files are still processed.

## What it recognizes

- **IPv4:** plain addresses, zero-padded addresses (`010.001.002.003`), `/len`, `addr mask` and `addr wildcard` pairs (`ip route`, `network … mask …`, ACL and OSPF wildcards), `host:port`, ranges like `a-b`, and AWS `ip-a-b-c-d` hostnames.
- **IPv6:** compressed, full and uppercase forms (the output keeps the input's style), `/len`, `%zone`, `[addr]:port` in URLs, and the IPv4-mapped `::ffff:a.b.c.d` and NAT64 `64:ff9b::a.b.c.d` forms (the embedded IPv4 uses the IPv4 mapping).
- **Reverse DNS:** full and partial `in-addr.arpa` and `ip6.arpa` names.
- **Left alone:**
  - Netmasks and wildcard masks, loopback, link-local, multicast and the unspecified address.
  - Version strings and OIDs (`1.2.3.4.5`), MAC addresses, timestamps, and `a::b`-style code.
  - Network expressions shorter than /8 (IPv4) or /16 (IPv6), such as `128.0.0.0/1`, since those identify nothing.

## File safety

- **Atomic writes:** output goes to a temporary file that is then renamed into place, so a crash or Ctrl-C never leaves a half-written file. In-place mode keeps the file's permissions.
- **Binary and old Excel files:** binary files, including old `.xls`, are detected and skipped rather than corrupted.
- **Encodings:** UTF-16 files (common in Windows exports) are handled, and other bytes pass through unchanged.
- **Overwrite protection:** the tool refuses to write over its own input (use `-i` for that) and refuses two inputs that would write to the same output.
- **Key and mapping files:** written with mode 600. A key shorter than 16 characters triggers a warning, because anyone holding one before/after pair could brute-force it.

## Limitations

- Redacted IPv4 addresses can fall in someone else's real public range. That is the price of keeping the structure.
- IPv6 written in a non-standard compressed form (for example `2001:db8:0::1`) comes out in the standard RFC 5952 form (`2001:db8::1`). The address values are always exact.
- If an address you excluded with `-x` coincides with a redacted output, the tool prints a warning, because `--reverse` would leave that address alone.
- `.docx`, `.pdf`, `.pcap` and other formats aren't supported yet. Pipe extracted text through stdin instead.

## Tests

```sh
python3 -m unittest test_ipredact -v
```
