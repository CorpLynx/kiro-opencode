# ipredact: structure-preserving IP redaction

Redacting addresses as `x.x.1.1` throws away structure. Afterwards you can't tell which hosts share a subnet or which route covers which network. `ipredact` instead **rewrites the network part of every address with different numbers** and keeps the host part:

- **IPv4:** the first two octets are rewritten and the last two are kept.
- **IPv6:** the routing prefix (the first 48 bits by default, the /48 site prefix) is rewritten. The subnet ID and interface ID are kept.

The output can't be used to reach real infrastructure, but it still reads like a real network.

It works on:

- **Text:** configs, logs, CSV.
- **Office files:** Word, Excel and PowerPoint.
- **Visio diagrams.**
- **LibreOffice files.**
- **Packet captures:** pcap and pcapng.

```
ip address 10.42.7.1 255.255.255.0             ->  ip address 68.232.7.1 255.255.255.0
ip route 10.0.0.0 255.0.0.0 172.16.4.1         ->  ip route 68.0.0.0 255.0.0.0 160.146.4.1
vpc 10.42.0.0/16 subnet 10.42.7.0/24           ->  vpc 68.232.0.0/16 subnet 68.232.7.0/24
ipv6 address 2001:db8:abcd:12::1/64            ->  ipv6 address 392a:9c05:adc0:12::1/64
ula fd12:3456:789a:1::10                       ->  ula fd05:2fb1:14bf:1::10
mapped ::ffff:10.42.7.15                       ->  mapped ::ffff:68.232.7.15
host ip-10-42-7-15.ec2.internal                ->  host ip-68-232-7-15.ec2.internal
15.7.42.10.in-addr.arpa.                       ->  15.7.232.68.in-addr.arpa.
mac 00:1a:2b:3c:4d:5e  time 12:34:56.789       ->  (untouched)
lo 127.0.0.1 ::1  md 169.254.169.254 fe80::1   ->  (untouched)
```

## Install

```sh
pip install './tools/ip-redactor[all]'   # text + Office/Visio/OpenDocument + captures
pip install ./tools/ip-redactor          # text only, no dependencies
```

| Extra | Adds | Needs |
| --- | --- | --- |
| *(none)* | text, CSV, logs, configs | Python 3.8+ standard library |
| `office` | `.docx` `.xlsx` `.pptx` `.vsdx` and their macro/template variants, `.odt` `.ods` `.odp` | `lxml` |
| `pcap` | `.pcap` `.pcapng` | `scapy` |
| `all` | everything above | |

Without installing, you can also run it as `python3 -m ipredact` from this directory. File types are detected from their contents, not their extension. If an input needs an extra that isn't installed, it is skipped with a message saying what to install.

## Usage

```sh
# stdin -> stdout, random key printed to stderr
ipredact < running-config.txt

# keep the key in a file so every run/file uses the same mapping
ipredact configs/ -d redacted/ --save-key redact.key
ipredact capture.pcapng network-diagram.vsdx ipam.xlsx -d redacted/ --key-file redact.key

# a lookup table of every real -> redacted address
ipredact dump.txt -o dump.red.txt --key-file redact.key --map-out mapping.csv

# undo (same key, same --v6-bits and exclude options)
ipredact -r --key-file redact.key redacted/capture.pcapng -o capture.pcapng
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
| `-v` | Print per-file details and a summary of counts to stderr. |

Exit status is 0 on success and 1 if any input was skipped. Skipped files are named on stderr and the remaining files are still processed.

## Guarantees

- **Consistent.** With a given key, the same address always becomes the same output, across lines, files, formats and runs. That holds across forms too: the address in a router config, the matching cell in an IPAM sheet, the label in a Visio diagram and the IP header in a capture all agree, as do `::ffff:10.42.7.15`, `ip-10-42-7-15` and the PTR name.
- **No collisions.** Two different addresses never redact to the same one.
- **Prefix-preserving.** Addresses that share a prefix still share it after redaction. For IPv4 that holds at /8 and longer; for IPv6 at any length. CIDR containment, summaries and route coverage stay true.
- **Always different.** For IPv4, the first and the second octet both change for every address. For IPv6, every rewritten hextet changes.
- **Output stays plausible.**
  - IPv4 output never lands in `0/8`, `127/8`, `255/8` or `169.254/16`.
  - IPv6 global unicast (`2000::/3`) stays global unicast, and ULA (`fc00::/7`) stays ULA.
  - Redacted addresses never become link-local or multicast.
- **Reversible with the key.** `--reverse` restores the originals. For captures this is byte for byte.

### How it works

- **IPv4 first octet:** a keyed shuffle of 1–254 (without 127) that leaves no value unchanged.
- **IPv4 second octet and the IPv6 prefix:** each bit is flipped by a keyed HMAC-SHA256 function of the original bits above it. This is the Crypto-PAn idea, limited to the network bits.
- **Key secrecy:** without the key, you can't work out the mapping from the output.

**One exception per key:** if one /16 happens to map onto `169.254/16`, it is moved to the slot `169.254` would have had. That /16 is still unique and reversible, but its /8 grouping is not preserved.

## What it covers

### Text

- **IPv4:** plain addresses, zero-padded addresses (`010.001.002.003`), `/len`, `addr mask` and `addr wildcard` pairs (`ip route`, `network … mask …`, ACL and OSPF wildcards), `host:port`, ranges like `a-b`, and AWS `ip-a-b-c-d` hostnames.
- **IPv6:** compressed, full and uppercase forms (the output keeps the input's style), `/len`, `%zone`, `[addr]:port`, and the IPv4-mapped and NAT64 forms.
- **Reverse DNS:** full and partial `in-addr.arpa` and `ip6.arpa` names.
- **Encodings:** UTF-16 files are handled, and other bytes pass through unchanged.
- **Left alone:**
  - Netmasks and wildcard masks, loopback, link-local, multicast.
  - Version strings and OIDs (`1.2.3.4.5`), MAC addresses, timestamps, `a::b`-style code.
  - Network expressions shorter than /8 (IPv4) or /16 (IPv6).

### Office, Visio and LibreOffice (`office` extra)

The tool edits the XML inside the file directly. Nothing the file contains is dropped, unlike opening and re-saving it with openpyxl or python-docx, which lose charts, shapes and pivot tables. It covers:

- **Text everywhere:** body text, tables, headers and footers, text boxes, comments, tracked insertions and deletions, field codes, and slide notes.
- **Spreadsheets:** cell text, formulas, cached formula results, and sheet names. Formulas that reference a sheet are updated to match its new name.
- **Hyperlink targets.**
- **Chart data.**
- **Visio:** shape text and shape data (for example an `IP Address` property on a router shape).
- **Embedded files:** an Excel sheet inside a Word document, or chart data inside a deck, is redacted too.
- **Addresses split across formatting runs.** Word and PowerPoint often store `10.1.2.3` as `10.1.` plus `2.3` after an edit or spell-check. Text is matched per paragraph, and the formatting of each piece is kept.
- **Thumbnail previews** (`docProps/thumbnail.*`) are **removed**, because they are a picture of the first page or slide, addresses included.
- **Not inspected:** images, embedded binary OLE objects and VBA macro code. If a file contains any of these, the tool prints a warning.
- **Untrusted files:** XML is parsed with entity expansion and network access turned off, and packages that would expand to more than 2 GiB are refused.

### Packet captures (`pcap` extra)

- **Rewritten:**
  - Every field scapy decodes as an address: IPv4 and IPv6 headers (including headers quoted inside ICMP errors and tunnelled packets), ARP, BOOTP and DHCP fields and options, DNS A and AAAA answers, ICMP redirects, and routing protocol fields.
  - Reverse-DNS names in DNS queries and answers.
  - Addresses in plain-text UDP payloads, such as syslog.
  - pcapng packet comments.
- **Checksums** are recalculated on every changed packet, so Wireshark shows no errors. Unchanged packets are written back byte for byte, and timestamps are kept.
- **TCP payloads** (an HTTP `Host:` header, for example) are rewritten only when the new address has the same number of characters, because changing the length would break TCP sequence numbers. Any address left in place this way is reported as a warning.
- **Truncated packets** (a small snaplen) get their headers rewritten and the IP header checksum fixed. The original on-the-wire lengths are kept.
- **pcapng output:**
  - Interface descriptions and name-resolution blocks are not carried over. Dropping them is safe, because they can contain names and addresses.
  - Packets keep their timestamps and comments.
- **Not rewritten:** MAC addresses. They are hardware addresses, not IP addresses.

## Limitations

- **PDF isn't supported.** Rewriting text in a PDF reliably isn't possible (font subsetting and glyph positioning get in the way), and getting it partly right would be worse than refusing. Redact the source document instead, or extract the text and pipe it through.
- **Images** inside documents (screenshots, for example) can't be redacted.
- **Real ranges:** redacted IPv4 addresses can fall in someone else's real public range. That is the price of keeping the structure.
- **IPv6 formatting:** IPv6 written in a non-standard compressed form (`2001:db8:0::1`) comes out in the standard RFC 5952 form. The address values are always exact.
- **Custom excludes:** if an address you excluded with `-x` coincides with a redacted output, the tool prints a warning, because `--reverse` would leave that address alone.

## Tests

```sh
pip install './tools/ip-redactor[test]'
cd tools/ip-redactor && python3 -m unittest discover -s tests -v
```

Office and capture tests are skipped automatically if their libraries aren't installed.
