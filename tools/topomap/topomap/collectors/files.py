"""Collect from saved device command output and config backups, without touching devices.

Owned by the parsers workstream; see DESIGN.md. Every file is read with
`parsers.parse_file`, facts about the same device from several files are
merged, and `facts.facts_to_topology` draws the result, exactly as the live
crawler does.
"""

from __future__ import annotations

import csv
import fnmatch
import ipaddress
import os
from datetime import datetime, timezone
from typing import Dict, Iterator, List, Optional, Sequence

from ..facts import DeviceFacts, device_id, facts_to_topology, normalize_hostname
from ..model import Topology
from ..parsers import PLATFORMS, ParseError, fill_from_neighbors, merge_facts, parse_file

HELP = "Collect from saved device command output and config backups, without touching devices."
PRODUCES_TOPOLOGY = True

# Files in a backup directory that are documentation or data, not device output.
SKIP_SUFFIXES = (".md", ".rst", ".csv", ".json", ".yaml", ".yml", ".py", ".html", ".xml")


def add_arguments(parser) -> None:
    parser.add_argument("paths", nargs="+", metavar="PATH",
                        help="saved command output or config files, or directories of them "
                             "(read recursively, skipping hidden and binary files)")
    parser.add_argument("--platform", choices=PLATFORMS,
                        help="platform to assume when a file doesn't make it obvious")
    parser.add_argument("--site", metavar="NAME", help="put every device in this site")
    parser.add_argument("--site-map", metavar="CSV",
                        help="CSV of hostname,site rows (hostnames may use * wildcards); "
                             "overrides --site for the devices it names")
    parser.add_argument("--no-l3", action="store_true",
                        help="don't infer L3 links and segments from shared subnets")
    parser.add_argument("--l3-exclude", action="append", default=[], metavar="CIDR",
                        help="ignore subnets inside CIDR when inferring L3 links, e.g. the "
                             "management network (repeatable)")
    parser.add_argument("--glob", action="append", default=[], metavar="PATTERN",
                        help="only read files whose name matches PATTERN, e.g. '*.txt' "
                             "(repeatable)")


def run(args) -> Topology:
    for cidr in args.l3_exclude:
        try:
            ipaddress.ip_network(cidr, strict=False)
        except ValueError:
            raise ValueError(f"--l3-exclude: not a network: {cidr!r}") from None
    site_map = read_site_map(args.site_map) if args.site_map else {}
    warnings: List[str] = []
    facts: List[DeviceFacts] = []
    parsed = 0
    for path in iter_files(args.paths, args.glob, warnings):
        text = read_text(path)
        if text is None:
            warnings.append(f"{path}: skipped, binary file")
            continue
        try:
            facts.extend(parse_file(text, args.platform, path))
            parsed += 1
        except ParseError as e:
            warnings.append(f"{path}: {e}")

    facts = merge_facts(facts)
    fill_from_neighbors(facts)
    for f in facts:
        f.site = site_for(f.hostname, site_map) or args.site or f.site
    topo = facts_to_topology(facts, infer_l3=not args.no_l3, l3_exclude=args.l3_exclude)
    for f in facts:   # merged facts carry "a; b" in .source; store the list
        node = topo.nodes[device_id(f.hostname)]
        node.attrs["sources"] = f.source.split("; ") if f.source else []
    topo.meta = {"collector": "files", "files": parsed, "devices": len(facts),
                 "collected_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if not parsed:
        warnings.append("no device output or configuration found")
    for w in warnings:
        topo.warn(w)
    return topo


def iter_files(paths: Sequence[str], globs: Sequence[str] = (),
               warnings: Optional[List[str]] = None) -> Iterator[str]:
    """Files to read, in a stable order. Named files are always read;
    directories are walked recursively, skipping hidden entries and the
    documentation/data suffixes in SKIP_SUFFIXES. A missing path is a bad
    argument, so it raises."""
    for path in paths:
        if os.path.isfile(path):
            if _wanted(path, globs):
                yield path
        elif os.path.isdir(path):
            for root, dirs, files in os.walk(path):
                dirs[:] = sorted(d for d in dirs if not d.startswith("."))
                for name in sorted(files):
                    full = os.path.join(root, name)
                    if not name.startswith(".") and not name.lower().endswith(SKIP_SUFFIXES) \
                            and _wanted(full, globs):
                        yield full
        else:
            raise ValueError(f"no such file or directory: {path}")


def _wanted(path: str, globs: Sequence[str]) -> bool:
    return not globs or any(fnmatch.fnmatch(os.path.basename(path), g) for g in globs)


def read_text(path: str) -> Optional[str]:
    """The file's text, or None if it looks binary (a NUL byte early on).
    Undecodable bytes are replaced: saved captures are often not clean UTF-8."""
    with open(path, "rb") as f:
        data = f.read()
    if b"\0" in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def read_site_map(path: str) -> Dict[str, str]:
    """hostname -> site from a CSV of hostname,site rows. A header row and
    blank or "#" lines are ignored."""
    out: Dict[str, str] = {}
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if len(row) < 2 or not row[0].strip() or row[0].strip().startswith("#"):
                continue
            host, site = row[0].strip(), row[1].strip()
            if host.lower() in ("hostname", "host", "device") and site.lower() == "site":
                continue
            out[host.lower() if any(c in host for c in "*?[") else normalize_hostname(host)] = site
    return out


def site_for(hostname: str, site_map: Dict[str, str]) -> str:
    name = normalize_hostname(hostname)
    if name in site_map:
        return site_map[name]
    for pattern, site in site_map.items():
        if any(c in pattern for c in "*?[") and fnmatch.fnmatchcase(name, pattern):
            return site
    return ""
