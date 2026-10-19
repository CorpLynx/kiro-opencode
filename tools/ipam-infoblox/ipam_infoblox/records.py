"""Turn a table's rows into clean CIDR records, noting every change made.

What an Infoblox import chokes on is fixed here, and each fix is reported so
nothing changes silently:

* blanks written as "-", "n/a" or "None" become empty;
* a cell holding several CIDRs (a VPC's secondary blocks) becomes one record each;
* a CIDR with host bits set (10.0.0.5/16) becomes its network (10.0.0.0/16);
* addresses rather than networks (Elastic IPs, ENIs) are dropped unless kept
  with --keep-type, and so are rows with no usable CIDR.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple, Union

from .reader import Table

Network = Union[ipaddress.IPv4Network, ipaddress.IPv6Network]

BLANKS = {"", "-", "--", "—", "n/a", "na", "none", "null", "(none)"}

# Resource types as AWS and its console spell them -> one name each.
TYPES = {
    "vpc": "vpc", "subnet": "subnet",
    "eip": "eip", "elasticip": "eip", "elasticipaddress": "eip",
    "eni": "eni", "networkinterface": "eni",
    "publicipv4pool": "public-ipv4-pool", "byoip": "public-ipv4-pool",
    "ipv6pool": "ipv6-pool",
    "pool": "pool", "ipampool": "pool", "toplevelpool": "pool", "regionalpool": "pool",
    "allocation": "allocation", "customallocation": "allocation",
}
CONTAINER_TYPES = frozenset({"pool", "vpc", "public-ipv4-pool", "ipv6-pool"})
ADDRESS_TYPES = frozenset({"eip", "eni"})
_SPLIT = re.compile(r"[\s,;]+")


@dataclass
class Record:
    """One CIDR from the export, with what is known about it."""
    row: int
    cidr: Network
    resource_type: str = ""
    resource_id: str = ""
    name: str = ""
    region: str = ""
    account: str = ""
    vpc_id: str = ""
    pool_id: str = ""
    description: str = ""
    raw: Dict[str, str] = field(default_factory=dict)

    def value(self, source: str) -> str:
        """A field ("account"), a column ("column:Cost Center") or a fixed
        text ("text:AWS"), as used by --ea and --comment."""
        if source.startswith("column:"):
            return self.raw.get(source[len("column:"):], "")
        if source.startswith("text:"):
            return source[len("text:"):]
        if source == "cidr":
            return str(self.cidr)
        return str(getattr(self, source, "") or "")

    @property
    def label(self) -> str:
        what = self.resource_id or self.name or str(self.cidr)
        return f"row {self.row} ({what})"


@dataclass
class Issue:
    row: int
    action: str          # "dropped" or "changed"
    message: str


def clean(value: Optional[str]) -> str:
    text = " ".join(str(value or "").split())
    return "" if text.lower() in BLANKS else text


def normalize_type(value: str) -> str:
    key = re.sub(r"[^a-z0-9]", "", value.lower())
    return TYPES.get(key, re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-"))


def parse_cidrs(text: str) -> Tuple[List[Network], List[str]]:
    """The CIDRs in a cell, and what was wrong with the parts that weren't."""
    found: List[Network] = []
    problems: List[str] = []
    for part in [p for p in _SPLIT.split(text) if p]:
        if "/" not in part:
            problems.append(f"{part!r} has no prefix length")
            continue
        try:
            found.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            problems.append(f"{part!r} is not a CIDR")
    return found, problems


def load_records(table: Table, keep_types: Iterable[str] = (),
                 drop_types: Iterable[str] = ()) -> Tuple[List[Record], List[Issue]]:
    """Clean records from a table whose columns are mapped, and the issues
    found on the way."""
    columns = table.columns
    keep = {normalize_type(t) for t in keep_types}
    drop = (set(ADDRESS_TYPES) - keep) | {normalize_type(t) for t in drop_types}
    records: List[Record] = []
    issues: List[Issue] = []
    seen = set()

    def get(values: Dict[str, str], name: str) -> str:
        return clean(values.get(columns[name], "")) if name in columns else ""

    for number, values in table.rows:
        cell = get(values, "cidr")
        if not cell:
            issues.append(Issue(number, "dropped", "no CIDR"))
            continue
        rtype = normalize_type(get(values, "resource_type"))
        if rtype in drop:
            issues.append(Issue(number, "dropped", f"{rtype} is an address, not a network "
                                f"(keep with --keep-type {rtype})" if rtype in ADDRESS_TYPES
                                else f"type {rtype} dropped by --drop-type"))
            continue
        cidrs, problems = parse_cidrs(cell)
        for problem in problems:
            issues.append(Issue(number, "dropped", problem))
        if len(cidrs) > 1:
            issues.append(Issue(number, "changed", f"{len(cidrs)} CIDRs in one cell, split into "
                                                   "one network each"))
        for cidr in cidrs:
            written = [p for p in _SPLIT.split(cell) if p and "/" in p]
            if not any(_same_text(w, cidr) for w in written):
                issues.append(Issue(number, "changed", f"host bits cleared: now {cidr}"))
            record = Record(
                row=number, cidr=cidr, resource_type=rtype,
                resource_id=get(values, "resource_id"), name=get(values, "name"),
                region=get(values, "region"), account=get(values, "account"),
                vpc_id=get(values, "vpc_id"), pool_id=get(values, "pool_id"),
                description=get(values, "description"),
                raw={k: clean(v) for k, v in values.items()})
            key = (cidr, rtype, record.resource_id, record.account, record.name)
            if key in seen:
                issues.append(Issue(number, "dropped", f"repeats an earlier row for {cidr}"))
                continue
            seen.add(key)
            records.append(record)
    return records, issues


def _same_text(text: str, cidr: Network) -> bool:
    try:
        return ipaddress.ip_network(text, strict=True) == cidr
    except ValueError:
        return False


def counts_by_type(records: Sequence[Record]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for record in records:
        key = record.resource_type or "(no type)"
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))
