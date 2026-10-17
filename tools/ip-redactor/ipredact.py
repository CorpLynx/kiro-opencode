#!/usr/bin/env python3
"""Structure-preserving IPv4 redaction.

Rewrites the first two octets of every IPv4 address with different numbers
while leaving the last two octets untouched, so redacted output keeps its
shape (same subnets, same hosts, same relationships) but no longer points at
real infrastructure.

The mapping is a keyed, prefix-preserving bijection over the top 16 bits
(the same idea as Crypto-PAn, restricted to two octets):

  * Consistent  - the same input always becomes the same output for a key,
                  across lines, files and runs.
  * One-to-one  - two different addresses never collide.
  * Prefix-preserving - if two addresses share their first N bits, their
                  redacted forms share their first N bits too. CIDR
                  containment (/8, /12, /16, /24, ...) survives redaction.
  * Always different - both the first and the second octet are guaranteed
                  to change for every address.
  * Reversible  - with the key, --reverse restores the original addresses.
"""

import argparse
import csv
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import sys

OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
IPV4 = rf"{OCTET}\.{OCTET}\.{OCTET}\.{OCTET}"

# An address, optionally followed by a CIDR length or by a dotted mask
# ("10.0.0.0 255.0.0.0", "network 10.0.0.0 mask 255.0.0.0",
# "permit 10.0.0.0 0.255.255.255"). The trailing mask decides whether a
# network address needs re-aligning after its prefix bits are rewritten.
ADDR_RE = re.compile(
    rf"(?<![\d.])(?P<ip>{IPV4})"
    rf"(?:/(?P<len>3[0-2]|[12]?\d)(?!\d)"
    rf"|(?P<sep>[ \t]+(?:mask[ \t]+)?)(?P<mask>{IPV4}))?"
    rf"(?!\.?\d)",
    re.IGNORECASE,
)

# AWS-style hostnames embed the address with dashes: ip-10-1-2-3.ec2.internal
DASHED_RE = re.compile(
    rf"(?<=ip-)(?P<ip>{OCTET}-{OCTET}-{OCTET}-{OCTET})(?![\d-]*\d)",
    re.IGNORECASE,
)

DEFAULT_EXCLUDES = (
    "0.0.0.0/8",        # "this network", wildcard masks like 0.0.0.255
    "127.0.0.0/8",      # loopback
    "169.254.0.0/16",   # link-local, incl. the 169.254.169.254 metadata IP
    "255.0.0.0/8",      # netmasks and broadcast
)

# Bits 0 and 8 of the 16-bit prefix are always flipped, which guarantees
# both the first and second octet differ from the original.
FORCED_FLIPS = (0, 8)

KEY_ENV = "IPREDACT_KEY"


def mask_len(value):
    """Prefix length of a contiguous netmask or wildcard mask, else None."""
    if value == 0:
        return 32  # 0.0.0.0 next to an address is a host wildcard
    if value >> 31:
        inverted = ~value & 0xFFFFFFFF
        if inverted & (inverted + 1) == 0:
            return 32 - inverted.bit_length()
        return None
    if value & (value + 1) == 0:
        return 32 - value.bit_length()
    return None


class Redactor:
    def __init__(self, key, excludes=DEFAULT_EXCLUDES, dashed=True):
        if isinstance(key, str):
            key = key.encode()
        if not key:
            raise ValueError("key must not be empty")
        self._key = key
        self._excludes = [ipaddress.ip_network(n, strict=False) for n in excludes]
        self._dashed = dashed
        self._flip_cache = {}
        self._fwd = {}
        self._rev = {}
        self.seen = {}      # original address -> redacted address
        self.collisions = set()  # redacted outputs that landed in an excluded range

    # -- the 16-bit prefix map -------------------------------------------

    def _flip(self, bit, prefix):
        """Keyed pseudorandom bit for position `bit` given the bits above it."""
        if bit in FORCED_FLIPS:
            return 1
        cache_key = (bit, prefix)
        if cache_key not in self._flip_cache:
            digest = hmac.new(
                self._key, bytes([bit]) + prefix.to_bytes(2, "big"), hashlib.sha256
            ).digest()
            self._flip_cache[cache_key] = digest[0] & 1
        return self._flip_cache[cache_key]

    def map16(self, value):
        if value not in self._fwd:
            out = 0
            for bit in range(16):
                shift = 15 - bit
                prefix = value >> (shift + 1)  # original bits above this one
                out |= (((value >> shift) & 1) ^ self._flip(bit, prefix)) << shift
            self._fwd[value] = out
            self._rev[out] = value
        return self._fwd[value]

    def unmap16(self, value):
        if value not in self._rev:
            orig = 0
            for bit in range(16):
                shift = 15 - bit
                prefix = orig >> (shift + 1)
                orig |= (((value >> shift) & 1) ^ self._flip(bit, prefix)) << shift
            self._fwd[orig] = value
            self._rev[value] = orig
        return self._rev[value]

    # -- whole addresses --------------------------------------------------

    def is_excluded(self, addr):
        ip = ipaddress.IPv4Address(addr)
        return any(ip in net for net in self._excludes)

    def map_int(self, addr, reverse=False, prefix_len=None):
        """Map a 32-bit address. If it is the network address of a prefix
        shorter than /16, the result is re-aligned to that prefix so it is
        still a valid network address containing all the mapped hosts."""
        top, low = addr >> 16, addr & 0xFFFF
        top = self.unmap16(top) if reverse else self.map16(top)
        out = (top << 16) | low
        if prefix_len is not None and prefix_len < 16:
            netmask = (0xFFFFFFFF << (32 - prefix_len)) & 0xFFFFFFFF
            if addr & ~netmask & 0xFFFFFFFF == 0:
                out &= netmask
        return out

    def map_ip(self, ip, reverse=False, prefix_len=None):
        if self.is_excluded(ip):
            return ip
        out = str(ipaddress.IPv4Address(
            self.map_int(int(ipaddress.IPv4Address(ip)), reverse, prefix_len)
        ))
        if not reverse:
            self.seen[ip] = out
            if self.is_excluded(out):
                self.collisions.add(out)
        return out

    # -- text -------------------------------------------------------------

    def redact_text(self, text, reverse=False):
        def addr_sub(m):
            ip, plen, sep, mask = m.group("ip", "len", "sep", "mask")
            if plen is not None:
                return f"{self.map_ip(ip, reverse, int(plen))}/{plen}"
            if mask is not None:
                mlen = mask_len(int(ipaddress.IPv4Address(mask)))
                if mlen is None:
                    # Not a mask, just two addresses in a row.
                    return f"{self.map_ip(ip, reverse)}{sep}{self.map_ip(mask, reverse)}"
                return f"{self.map_ip(ip, reverse, mlen)}{sep}{mask}"
            return self.map_ip(ip, reverse)

        text = ADDR_RE.sub(addr_sub, text)
        if self._dashed:
            text = DASHED_RE.sub(
                lambda m: self.map_ip(m.group("ip").replace("-", "."), reverse).replace(".", "-"),
                text,
            )
        return text


# -- file handling ------------------------------------------------------------

def process_xlsx(redactor, src, dst, reverse):
    try:
        import openpyxl
    except ImportError:
        sys.exit("error: .xlsx support needs openpyxl (pip install openpyxl)")
    wb = openpyxl.load_workbook(src)
    for ws in wb.worksheets:
        for row in ws.iter_rows():
            for cell in row:
                if isinstance(cell.value, str):
                    cell.value = redactor.redact_text(cell.value, reverse)
    wb.save(dst)


def read_text(path):
    if path == "-":
        return sys.stdin.read()
    with open(path, encoding="utf-8", errors="surrogateescape", newline="") as f:
        return f.read()


def write_text(path, text):
    if path == "-":
        sys.stdout.write(text)
        return
    with open(path, "w", encoding="utf-8", errors="surrogateescape", newline="") as f:
        f.write(text)


def output_path(src, args):
    if args.in_place:
        return src
    if args.output:
        return args.output
    if args.out_dir:
        os.makedirs(args.out_dir, exist_ok=True)
        return os.path.join(args.out_dir, os.path.basename(src))
    return "-"


def resolve_key(args):
    if args.key:
        return args.key, False
    if args.key_file:
        with open(args.key_file) as f:
            return f.read().strip(), False
    if os.environ.get(KEY_ENV):
        return os.environ[KEY_ENV], False
    if args.reverse:
        sys.exit(f"error: --reverse needs the key used to redact (--key, --key-file or ${KEY_ENV})")
    return secrets.token_hex(16), True


def build_parser():
    p = argparse.ArgumentParser(
        description="Redact IPv4 addresses by rewriting their first two octets "
                    "while keeping subnet structure and host relationships intact.",
    )
    p.add_argument("files", nargs="*", default=["-"],
                   help="text, CSV or .xlsx files to redact (default: stdin)")
    out = p.add_mutually_exclusive_group()
    out.add_argument("-o", "--output", help="output file (single input only)")
    out.add_argument("-d", "--out-dir", help="write each redacted file into this directory")
    out.add_argument("-i", "--in-place", action="store_true", help="overwrite the input files")
    p.add_argument("-k", "--key", help=f"secret key for the mapping (or set ${KEY_ENV})")
    p.add_argument("--key-file", help="read the secret key from this file")
    p.add_argument("--save-key", help="write the key that was used to this file")
    p.add_argument("-r", "--reverse", action="store_true",
                   help="restore original addresses (needs the redaction key)")
    p.add_argument("-x", "--exclude", action="append", default=[], metavar="CIDR",
                   help="leave addresses in this network untouched (repeatable)")
    p.add_argument("--no-default-excludes", action="store_true",
                   help="also redact " + ", ".join(DEFAULT_EXCLUDES))
    p.add_argument("--no-dashed", action="store_true",
                   help="don't redact dashed hostnames like ip-10-1-2-3")
    p.add_argument("--map-out", metavar="CSV",
                   help="write an original,redacted CSV of every address seen "
                        "(sensitive: contains the real addresses)")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.output and len(args.files) > 1:
        sys.exit("error: --output takes a single input; use --out-dir for several")
    if (args.in_place or args.out_dir) and "-" in args.files:
        sys.exit("error: --in-place/--out-dir need file arguments, not stdin")
    for net in args.exclude:
        try:
            ipaddress.ip_network(net, strict=False)
        except ValueError:
            sys.exit(f"error: bad --exclude network: {net}")

    key, generated = resolve_key(args)
    excludes = list(args.exclude)
    if not args.no_default_excludes:
        excludes += DEFAULT_EXCLUDES
    redactor = Redactor(key, excludes, dashed=not args.no_dashed)

    for src in args.files:
        dst = output_path(src, args)
        if src.lower().endswith(".xlsx"):
            if dst == "-":
                sys.exit(f"error: {src}: .xlsx output needs -o, -d or -i")
            process_xlsx(redactor, src, dst, args.reverse)
        else:
            write_text(dst, redactor.redact_text(read_text(src), args.reverse))

    if args.save_key:
        with open(args.save_key, "w") as f:
            f.write(key + "\n")
    if generated and not args.save_key:
        print(f"ipredact: generated key {key} - pass it with --key to keep the "
              f"same mapping across runs or to --reverse", file=sys.stderr)
    if args.map_out:
        with open(args.map_out, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["original", "redacted"])
            for orig, red in sorted(redactor.seen.items(),
                                    key=lambda kv: ipaddress.IPv4Address(kv[0])):
                w.writerow([orig, red])
    if redactor.collisions:
        print(f"ipredact: warning: {len(redactor.collisions)} redacted address(es) "
              f"landed in an excluded range ({', '.join(sorted(redactor.collisions))}); "
              f"--reverse will leave those as-is", file=sys.stderr)


if __name__ == "__main__":
    main()
