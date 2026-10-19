"""Decide what each CIDR becomes in Infoblox, and whether it can go in.

Infoblox holds two kinds of range: a *network container* holds other ranges,
and a *network* holds addresses. A network can't hold networks, so the
choice matters:

* IPAM pools and VPCs become containers and subnets become networks, and
  anything that holds other ranges, from the export or already in the Grid,
  is promoted to a container.
* The same CIDR twice in one network view is merged when it is the same
  range seen twice (a pool allocation and the VPC using it), and a conflict
  when two VPCs or subnets overlap (other accounts' 10.0.0.0/16s); those
  need a network view each (`--network-view 'aws-{account}'`).

With the Grid's current contents, each range is then new (imported), already
there (left out), or in conflict (left out and explained): inside an
existing network, or a container where the Grid has a network that the
export puts networks into.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .records import CONTAINER_TYPES, Issue, Network, Record
from .wapi import Existing

COMMENT_LIMIT = 256
DEFAULT_COMMENT = "{name} ({resource_id})"
# Which record of a merged CIDR describes it, most specific first.
_PRIMARY = ("subnet", "vpc", "allocation", "public-ipv4-pool", "ipv6-pool", "pool")
_FIELD = re.compile(r"\{([a-z_]+(?::[^{}]*)?)\}")


@dataclass
class Options:
    view: str = "default"                                        # may hold {account}, {region}, ...
    eas: List[Tuple[str, str]] = field(default_factory=list)      # (EA name, source)
    comment: str = DEFAULT_COMMENT


@dataclass
class Entry:
    view: str
    cidr: Network
    records: List[Record]
    kind: str = "network"                     # network | container
    status: str = "new"                       # new | exists | conflict
    reasons: List[str] = field(default_factory=list)   # why a conflict
    notes: List[str] = field(default_factory=list)
    parent: str = ""                          # what it goes inside, in words
    parent_cidr: Optional[Network] = None     # the export's range it goes inside

    @property
    def primary(self) -> Record:
        def rank(r: Record) -> Tuple[int, int]:
            order = _PRIMARY.index(r.resource_type) if r.resource_type in _PRIMARY else len(_PRIMARY)
            return order, r.row
        return min(self.records, key=rank)

    @property
    def rows(self) -> str:
        return ", ".join(str(r.row) for r in sorted(self.records, key=lambda r: r.row))

    def sort_key(self) -> Tuple[int, int, int, str]:
        return (self.cidr.version, int(self.cidr.network_address), self.cidr.prefixlen, self.view)


@dataclass
class Plan:
    entries: List[Entry]
    issues: List[Issue] = field(default_factory=list)       # rows left out while planning
    problems: List[str] = field(default_factory=list)       # Grid-wide: missing views, EAs
    checked: bool = False                                   # compared with a Grid

    def by_status(self, status: str) -> List[Entry]:
        return sorted((e for e in self.entries if e.status == status), key=Entry.sort_key)

    @property
    def blocked(self) -> bool:
        return bool(self.by_status("conflict") or self.problems)


# -- templates ---------------------------------------------------------------------

def render_template(template: str, record: Record) -> str:
    """Fill {field} and {column:Header} from a record, then tidy what blank
    fields leave behind ("" in "() vpc-1" style leftovers)."""
    def value(m: "re.Match[str]") -> str:
        key = m.group(1)
        return record.value(key if key.startswith("column:") else key)
    text = _FIELD.sub(value, template)
    text = re.sub(r"\(\s*\)|\[\s*\]", "", text)
    text = " ".join(text.split())
    if re.fullmatch(r"\([^()]*\)", text):
        text = text[1:-1].strip()
    return text


def blank_fields(template: str, record: Record) -> List[str]:
    """The fields a template uses that are blank in this record."""
    return [m.group(1) for m in _FIELD.finditer(template)
            if not record.value(m.group(1)).strip()]


def view_for(options: Options, record: Record) -> str:
    """The record's network view, or "" when the template needs a field
    the record doesn't have ("aws-{account}" on a pool with no owner must not
    become a view called "aws-")."""
    return "" if blank_fields(options.view, record) else render_template(options.view, record)


def check_template(template: str, what: str) -> None:
    for m in _FIELD.finditer(template):
        key = m.group(1)
        if not key.startswith("column:") and key not in Record.__dataclass_fields__:
            raise ValueError(f"{what}: unknown field {{{key}}} (fields: {', '.join(source_names())}, "
                             "or {column:Header})")


def source_names() -> List[str]:
    return [f for f in Record.__dataclass_fields__ if f not in ("row", "raw")]


def comment_for(entry: Entry, options: Options) -> str:
    text = render_template(options.comment, entry.primary)
    return text if len(text) <= COMMENT_LIMIT else text[:COMMENT_LIMIT - 1] + "…"


def ea_values(entry: Entry, options: Options) -> Dict[str, str]:
    return {name: entry.primary.value(source) for name, source in options.eas}


# -- planning ----------------------------------------------------------------------

def build_plan(records: Sequence[Record], options: Options,
               existing: Optional[Existing] = None) -> Plan:
    plan = Plan([], checked=existing is not None)
    groups: Dict[Tuple[str, Network], List[Record]] = {}
    for record in records:
        view = view_for(options, record)
        if not view:
            missing = blank_fields(options.view, record) or ["a value"]
            plan.issues.append(Issue(record.row, "dropped", f"--network-view {options.view!r} needs "
                                     f"{', '.join('{' + m + '}' for m in missing)}, which this "
                                     "row doesn't have"))
            continue
        groups.setdefault((view, record.cidr), []).append(record)
    for (view, cidr), members in sorted(groups.items(), key=lambda kv: (kv[0][0], kv[0][1].version,
                                                                        int(kv[0][1].network_address),
                                                                        kv[0][1].prefixlen)):
        plan.entries.append(_entry(view, cidr, members))
    _nest(plan.entries)
    if existing is not None:
        _compare(plan, existing)
        _check_grid(plan, options, existing)
    _describe_parents(plan)
    return plan


def _entry(view: str, cidr: Network, members: List[Record]) -> Entry:
    entry = Entry(view, cidr, sorted(members, key=lambda r: r.row))
    types = {r.resource_type for r in members}
    entry.kind = "container" if types & CONTAINER_TYPES else "network"
    if "subnet" in types and entry.kind == "container":
        # A VPC with one subnet the size of the VPC: as a container it could
        # hold no addresses. Nesting still promotes it if ranges sit inside.
        entry.kind = "network"
        entry.notes.append("a subnet covers the whole range, so it is imported as a network")
    clashing = {}
    for r in members:
        if r.resource_type in ("vpc", "subnet") and r.resource_id:
            clashing.setdefault(r.resource_type, set()).add((r.resource_id, r.account))
    for rtype, ids in sorted(clashing.items()):
        if len(ids) > 1:
            who = ", ".join(f"{rid}" + (f" (account {acct})" if acct else "")
                            for rid, acct in sorted(ids))
            entry.status = "conflict"
            entry.reasons.append(f"{len(ids)} {rtype}s use {cidr} in network view {view}: {who}. "
                                 "Overlapping ranges need separate network views, e.g. "
                                 "--network-view 'aws-{account}'")
    if len(members) > 1 and entry.status != "conflict":
        others = sorted({r.resource_type or "row" for r in members if r is not entry.primary})
        entry.notes.append(f"merged with the {', '.join(others)} on rows {entry.rows}")
    return entry


def _parent_of(cidr: Network, known: Dict[Network, Any]) -> Optional[Network]:
    for prefix in range(cidr.prefixlen - 1, -1, -1):
        candidate = cidr.supernet(new_prefix=prefix)
        if candidate in known:
            return candidate
    return None


def _nest(entries: List[Entry]) -> None:
    """Promote ranges that hold other ranges from the export to containers,
    and say what each one goes inside."""
    by_view: Dict[str, Dict[Network, Entry]] = {}
    for entry in entries:
        by_view.setdefault(entry.view, {})[entry.cidr] = entry
    for entry in entries:
        parent = _parent_of(entry.cidr, by_view[entry.view])
        if parent is None:
            continue
        holder = by_view[entry.view][parent]
        entry.parent, entry.parent_cidr = f"{parent} (from this export)", parent
        if holder.kind == "network":
            holder.kind = "container"
            holder.notes.append("imported as a container: other ranges in the export sit inside it")


class _Index:
    """The Grid's ranges in one view, for "what holds this" and "what's inside"."""

    def __init__(self, objects: Dict[Network, Dict[str, Any]]):
        self.objects = objects
        self.keys = sorted((c.version, int(c.network_address), c.prefixlen, c) for c in objects)

    def inside(self, cidr: Network) -> List[Network]:
        lo = bisect.bisect_left(self.keys, (cidr.version, int(cidr.network_address), -1))
        out = []
        for version, start, _, other in self.keys[lo:]:
            if version != cidr.version or start > int(cidr.broadcast_address):
                break
            if other != cidr and other.subnet_of(cidr):  # type: ignore[arg-type]
                out.append(other)
        return out


def _compare(plan: Plan, existing: Existing) -> None:
    indexes = {view: _Index(existing.in_view(view)) for view in {e.view for e in plan.entries}}
    for entry in plan.entries:
        index = indexes[entry.view]
        have = index.objects.get(entry.cidr)
        if have is not None:
            _against_same(entry, have, plan.entries)
            continue
        if entry.status == "conflict":
            continue
        holder = _parent_of(entry.cidr, index.objects)
        if holder is not None and index.objects[holder]["kind"] == "network":
            entry.status = "conflict"
            entry.reasons.append(f"it is inside {holder}, which is a network in Infoblox, and a "
                                 f"network can't hold other ranges: convert {holder} to a network "
                                 "container in Infoblox first")
            continue
        if holder is not None and not entry.parent:
            entry.parent = f"{holder} (already in Infoblox)"
        inside = index.inside(entry.cidr)
        if inside and entry.kind == "network":
            entry.kind = "container"
            entry.notes.append(f"imported as a container: Infoblox already has {len(inside)} "
                               f"range(s) inside it, such as {inside[0]}")


def _describe_parents(plan: Plan) -> None:
    """Say plainly when the export's range a new one goes inside isn't
    itself being imported."""
    status = {(e.view, e.cidr): e.status for e in plan.entries}
    for entry in plan.entries:
        if entry.parent_cidr is None:
            continue
        parent = status[(entry.view, entry.parent_cidr)]
        if parent == "exists":
            entry.parent = f"{entry.parent_cidr} (already in Infoblox)"
        elif parent == "conflict":
            entry.parent = f"{entry.parent_cidr} (from this export, but left out: see Fix before importing)"


def _against_same(entry: Entry, have: Dict[str, Any], entries: List[Entry]) -> None:
    """The Grid already has this exact CIDR in the view."""
    if entry.status == "conflict":
        return
    if have["kind"] == "network" and entry.kind == "container":
        children = [e for e in entries if e.view == entry.view and e.cidr != entry.cidr
                    and e.cidr.version == entry.cidr.version
                    and e.cidr.subnet_of(entry.cidr)]  # type: ignore[arg-type]
        if children:
            entry.status = "conflict"
            entry.reasons.append(f"Infoblox has {entry.cidr} as a network, but the export puts "
                                 f"{len(children)} range(s) inside it, such as {children[0].cidr}: "
                                 "convert it to a network container in Infoblox first")
            return
    entry.status = "exists"
    if have["kind"] != entry.kind:
        entry.notes.append(f"Infoblox has it as a {have['kind']}")
    if have.get("comment"):
        entry.notes.append(f"Infoblox comment: {have['comment']}")


def _check_grid(plan: Plan, options: Options, existing: Existing) -> None:
    """Things that would make the whole import fail: a network view or an
    extensible attribute the Grid doesn't have, or a value a list-type
    attribute doesn't allow."""
    for view in sorted({e.view for e in plan.by_status("new")}):
        if view in existing.views:
            continue
        missing = [e for e in plan.by_status("new") if e.view == view]
        for entry in missing:
            entry.status = "conflict"
            entry.reasons.append(f"network view {view!r} doesn't exist in the Grid")
        plan.problems.append(f"network view {view!r} doesn't exist in the Grid, so its "
                             f"{len(missing)} range(s) are left out; create the view, or choose "
                             "another with --network-view")
    importing = plan.by_status("new")
    for name, _ in options.eas:
        definition = existing.ea_defs.get(name)
        if definition is None:
            plan.problems.append(f"extensible attribute {name!r} isn't defined in the Grid; "
                                 "define it (Administration > Extensible Attributes) first")
            continue
        allowed = definition.get("values") or []
        if definition.get("type", "").upper() == "ENUM" and allowed:
            bad = sorted({v for e in importing for k, v in ea_values(e, options).items()
                          if k == name and v and v not in allowed})
            if bad:
                plan.problems.append(f"extensible attribute {name!r} only allows "
                                     f"{', '.join(map(str, allowed))}; the export has "
                                     f"{', '.join(bad[:5])}" + (" ..." if len(bad) > 5 else ""))
