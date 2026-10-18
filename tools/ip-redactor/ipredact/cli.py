"""Command-line interface."""

import argparse
import csv
import io
import ipaddress
import os
import secrets
import sys

from . import __version__
from .core import DEFAULT_EXCLUDES, V6_BITS_CHOICES, Redactor
from .files import SkipFile, detect, open_output, redact_text_file, write_bytes

KEY_ENV = "IPREDACT_KEY"
MIN_KEY_LEN = 16


def warn(msg):
    print(f"ipredact: {msg}", file=sys.stderr)


def handler_for(kind):
    if kind == "capture":
        from .capture import redact_capture_file
        return redact_capture_file
    if kind == "package":
        from .office import redact_package_file
        return redact_package_file
    return redact_text_file


def collect_inputs(paths):
    """Expand directories into (source, path relative to the argument)."""
    out = []
    for p in paths:
        if p == "-":
            out.append(("-", "-"))
        elif os.path.isdir(p):
            for root, dirs, files in os.walk(p):
                dirs.sort()
                for name in sorted(files):
                    full = os.path.join(root, name)
                    out.append((full, os.path.relpath(full, p)))
        else:
            out.append((p, os.path.basename(p)))
    return out


def plan_outputs(inputs, args):
    plan, seen = [], {}
    for src, rel in inputs:
        if args.in_place:
            dst = src
        elif args.output:
            dst = args.output
        elif args.out_dir:
            dst = os.path.join(args.out_dir, rel)
        else:
            dst = "-"
        if dst != "-" and not args.in_place:
            real = os.path.realpath(dst)
            if src != "-" and os.path.exists(dst) and os.path.samefile(src, dst):
                sys.exit(f"error: {src} would overwrite itself; use --in-place for that")
            if real in seen:
                sys.exit(f"error: {seen[real]} and {src} would both write {dst}")
            seen[real] = src
        plan.append((src, dst))
    return plan


def resolve_key(args):
    if args.key:
        return args.key, False
    if args.key_file:
        with open(args.key_file) as f:
            key = f.read().strip()
        if not key:
            sys.exit(f"error: key file {args.key_file} is empty")
        return key, False
    if os.environ.get(KEY_ENV):
        return os.environ[KEY_ENV], False
    if args.reverse:
        sys.exit(f"error: --reverse needs the key used to redact (--key, --key-file or ${KEY_ENV})")
    return secrets.token_hex(16), True


def process(redactor, src, dst, reverse):
    """Redact one input into dst; returns notes to show the user."""
    notes = []
    if src == "-":
        infile = io.BytesIO(sys.stdin.buffer.read())
    else:
        infile = open(src, "rb")
    with infile:
        kind = detect(infile.read(8))
        infile.seek(0)
        handler = handler_for(kind)
        with open_output(dst) as outfile:
            handler(redactor, infile, outfile, reverse, notes)
    return notes


def build_parser():
    p = argparse.ArgumentParser(
        prog="ipredact",
        description="Redact IPv4 and IPv6 addresses by rewriting their network prefix while "
                    "keeping subnet structure and host relationships intact. Handles text "
                    "(configs, logs, CSV), Office/Visio/OpenDocument files and pcap/pcapng captures.",
    )
    p.add_argument("files", nargs="*", default=["-"],
                   help="files or directories to walk (default: stdin)")
    out = p.add_mutually_exclusive_group()
    out.add_argument("-o", "--output", help="output file (single input only)")
    out.add_argument("-d", "--out-dir", help="write redacted files into this directory, "
                                             "mirroring the layout of directory inputs")
    out.add_argument("-i", "--in-place", action="store_true", help="overwrite the input files")
    p.add_argument("-k", "--key", help="secret key for the mapping; visible in the process "
                                       f"list, so prefer --key-file or ${KEY_ENV}")
    p.add_argument("--key-file", help="read the secret key from this file")
    p.add_argument("--save-key", help="write the key that was used to this file (mode 600)")
    p.add_argument("-r", "--reverse", action="store_true",
                   help="restore original addresses (needs the redaction key and the same "
                        "--v6-bits / exclude settings)")
    p.add_argument("-x", "--exclude", action="append", default=[], metavar="CIDR",
                   help="leave addresses in this IPv4 or IPv6 network untouched (repeatable)")
    p.add_argument("--no-default-excludes", action="store_true",
                   help="also redact " + ", ".join(DEFAULT_EXCLUDES))
    p.add_argument("--no-dashed", action="store_true",
                   help="don't redact dashed hostnames like ip-10-1-2-3")
    p.add_argument("--v6-bits", type=int, default=48, choices=V6_BITS_CHOICES,
                   help="how many leading IPv6 bits to rewrite (default 48: the site prefix)")
    p.add_argument("--map-out", metavar="CSV",
                   help="write an original,redacted CSV of every address seen "
                        "(mode 600; sensitive: contains the real addresses)")
    p.add_argument("-v", "--verbose", action="store_true", help="print a summary to stderr")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    inputs = collect_inputs(args.files)
    if args.output and (len(inputs) != 1 or any(os.path.isdir(f) for f in args.files)):
        sys.exit("error: --output takes a single input file; use --out-dir for several")
    if (args.in_place or args.out_dir) and "-" in args.files:
        sys.exit("error: --in-place/--out-dir need file arguments, not stdin")
    if not (args.in_place or args.out_dir) and any(os.path.isdir(f) for f in args.files):
        sys.exit("error: directory inputs need --out-dir or --in-place")
    for net in args.exclude:
        try:
            ipaddress.ip_network(net, strict=False)
        except ValueError:
            sys.exit(f"error: bad --exclude network: {net}")

    key, generated = resolve_key(args)
    if not generated and len(key) < MIN_KEY_LEN:
        warn(f"warning: key is shorter than {MIN_KEY_LEN} characters; anyone holding one "
             f"original/redacted pair could brute-force it and undo the redaction")
    excludes = list(args.exclude)
    if not args.no_default_excludes:
        excludes += DEFAULT_EXCLUDES
    redactor = Redactor(key, excludes, dashed=not args.no_dashed, v6_bits=args.v6_bits)

    failed = done = 0
    for src, dst in plan_outputs(inputs, args):
        try:
            notes = process(redactor, src, dst, args.reverse)
            done += 1
        except (SkipFile, OSError, UnicodeError) as e:
            failed += 1
            warn(f"skipped {src}: {e}")
            continue
        except Exception as e:  # e.g. a corrupt workbook; keep going with the rest
            failed += 1
            warn(f"skipped {src}: {type(e).__name__}: {e}")
            continue
        for note in notes:
            if not note.startswith("info: "):
                warn(f"{src}: {note}")
            elif args.verbose:
                warn(f"{src}: {note[6:]}")

    if args.save_key:
        write_bytes(args.save_key, (key + "\n").encode(), mode=0o600)
    if generated and not args.save_key:
        warn(f"generated key {key} - pass it with --key-file/--key to keep the "
             f"same mapping across runs or to --reverse")
    if args.map_out:
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(["original", "redacted"])
        for orig, red in sorted(redactor.seen.items(), key=lambda kv: (kv[0].version, kv[0])):
            w.writerow([orig, red])
        write_bytes(args.map_out, buf.getvalue().encode(), mode=0o600)
    if redactor.collisions:
        found = ", ".join(map(str, sorted(redactor.collisions, key=lambda a: (a.version, a))))
        warn(f"warning: {len(redactor.collisions)} redacted address(es) landed in an "
             f"excluded range ({found}); --reverse will leave those as-is")
    if args.verbose:
        action = "restored" if args.reverse else "redacted"
        warn(f"{action} {redactor.count} address(es) in {done} file(s)"
             + (f", {len(redactor.seen)} unique" if not args.reverse else "")
             + (f"; {failed} file(s) skipped" if failed else ""))
    return 1 if failed else 0
