"""Show what changed between two topology snapshots (exit status 1 if anything did).

Snapshots are sorted JSON, so `git diff` already shows *that* something
changed. This says *what*, in topology terms: which nodes and links
appeared, disappeared or changed, down to the attribute. Lists are compared
as sets because collectors don't promise an order, and lists of named
things (interfaces, route tables) are matched by name so one interface's
change reads as that, not as one item removed and another added.

Exit status follows diff(1): 0 identical, 1 different, 2 trouble. That lets
`topomapper diff` gate CI or detect drift between scheduled collections.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional

from .merge import read_topologies
from .model import Link, Node, Topology

HELP = "Show what changed between two topology snapshots (exit status 1 if anything did)."
PRODUCES_TOPOLOGY = False

# `sources` records which file or host a device came from: noise for drift.
DEFAULT_IGNORE = ("sources",)
# Fields that name an item in a list of dicts, tried in this order.
IDENTITY_KEYS = ("id", "name", "destination", "outside_ip", "vif_id")
ARROW = "→"
_ASCII = str.maketrans({"→": "->", "↔": "<->"})


class _Missing:
    def __repr__(self) -> str:
        return "MISSING"


MISSING: Any = _Missing()


@dataclass
class Change:
    """One difference inside a node or link. Set-like lists report the items
    `added` and `removed` instead of `old` / `new`."""
    path: str
    old: Any = MISSING
    new: Any = MISSING
    added: Optional[List[Any]] = None
    removed: Optional[List[Any]] = None

    @property
    def is_set_change(self) -> bool:
        return self.added is not None or self.removed is not None

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"path": self.path}
        if self.is_set_change:
            d["added"] = list(self.added or [])
            d["removed"] = list(self.removed or [])
            return d
        if self.old is not MISSING:
            d["old"] = self.old
        if self.new is not MISSING:
            d["new"] = self.new
        return d

    def __str__(self) -> str:
        if self.is_set_change:
            items = [f"+{_fmt(x)}" for x in self.added or []]
            items += [f"-{_fmt(x)}" for x in self.removed or []]
            return f"{self.path}: {', '.join(items)}"
        return f"{self.path}: {_fmt(self.old)} {ARROW} {_fmt(self.new)}"


@dataclass
class ItemChange:
    """A node or link present in both snapshots whose content differs."""
    id: str
    changes: List[Change]

    def to_dict(self) -> Dict[str, Any]:
        return {"id": self.id, "changes": [c.to_dict() for c in self.changes]}


@dataclass
class Section:
    added: List[str] = field(default_factory=list)
    removed: List[str] = field(default_factory=list)
    changed: List[ItemChange] = field(default_factory=list)

    def counts(self) -> Dict[str, int]:
        return {"added": len(self.added), "removed": len(self.removed),
                "changed": len(self.changed)}

    def __bool__(self) -> bool:
        return bool(self.added or self.removed or self.changed)


@dataclass
class DiffResult:
    old: Topology = field(repr=False)
    new: Topology = field(repr=False)
    nodes: Section = field(default_factory=Section)
    links: Section = field(default_factory=Section)

    @property
    def identical(self) -> bool:
        return not (self.nodes or self.links)

    def counts(self) -> Dict[str, Dict[str, int]]:
        return {"nodes": self.nodes.counts(), "links": self.links.counts()}

    def to_dict(self, summary: bool = False) -> Dict[str, Any]:
        """JSON form. Added and removed items are given whole, so a consumer
        doesn't need the snapshots to know what appeared or went away."""
        d: Dict[str, Any] = {"identical": self.identical, "counts": self.counts()}
        if summary:
            return d
        for what, old, new in (("nodes", self.old.nodes, self.new.nodes),
                               ("links", self.old.links, self.new.links)):
            section: Section = getattr(self, what)
            d[what] = {"added": [new[i].to_dict() for i in section.added],
                       "removed": [old[i].to_dict() for i in section.removed],
                       "changed": [c.to_dict() for c in section.changed]}
        return d

    def summary_line(self) -> str:
        if self.identical:
            return "no differences"
        return "; ".join(f"{what}: {c['added']} added, {c['removed']} removed, {c['changed']} changed"
                         for what, c in self.counts().items())

    def format_text(self, summary: bool = False) -> str:
        """Grouped added / removed / changed, nodes before links within each,
        so the big moves come first and attribute detail last."""
        lines = [self.summary_line()]
        if summary:
            return lines[0] + "\n"
        for verb, sign, topo in (("added", "+", self.new), ("removed", "-", self.old),
                                 ("changed", "~", self.new)):
            for what in ("nodes", "links"):
                items = getattr(getattr(self, what), verb)
                if not items:
                    continue
                lines += ["", f"{verb.capitalize()} {what}:"]
                for item in items:
                    item_id = item.id if isinstance(item, ItemChange) else item
                    describe = _describe_node if what == "nodes" else _describe_link
                    lines.append(f"  {sign} {describe(topo, item_id)}")
                    if isinstance(item, ItemChange):
                        lines += [f"      {change}" for change in item.changes]
        return "\n".join(lines) + "\n"


# -- comparing -----------------------------------------------------------------

def diff(old: Topology, new: Topology, ignore_attrs: Iterable[str] = DEFAULT_IGNORE) -> DiffResult:
    """Compare two topologies by node and link ID. `meta` is never compared:
    it holds collection times and warnings, which change on every run.

    `ignore_attrs` names are dropped before comparing. A bare name ("state")
    matches that key at any depth, a dotted one ("tags.LastSeen") the path
    to it; list levels add nothing to the path, so "interfaces.status"
    reaches into every interface. "label", "parent" and the other node and
    link fields can be ignored the same way.
    """
    ignore = frozenset(ignore_attrs)
    result = DiffResult(old, new)
    _diff_items(old.nodes, new.nodes, _node_fields, ignore, result.nodes)
    _diff_items(old.links, new.links, _link_fields, ignore, result.links)
    return result


def _node_fields(node: Node) -> Dict[str, Any]:
    return {"kind": node.kind, "label": node.label, "parent": node.parent}


def _link_fields(link: Link) -> Dict[str, Any]:
    return {"kind": link.kind, "source": link.source, "target": link.target,
            "label": link.label, "directed": link.directed}


def _diff_items(old: Dict[str, Any], new: Dict[str, Any], fields: Callable[[Any], Dict[str, Any]],
                ignore: frozenset, section: Section) -> None:
    section.added = sorted(set(new) - set(old))
    section.removed = sorted(set(old) - set(new))
    for item_id in sorted(set(old) & set(new)):
        a, b = old[item_id], new[item_id]
        changes: List[Change] = []
        _compare(_strip(fields(a), ignore), _strip(fields(b), ignore), "", changes)
        _compare(_strip(a.attrs, ignore), _strip(b.attrs, ignore), "", changes)
        if changes:
            section.changed.append(ItemChange(item_id, changes))


def _strip(value: Any, ignore: frozenset, path: str = "") -> Any:
    """Copy of `value` without ignored keys, at any depth."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            sub = f"{path}.{key}" if path else str(key)
            if key not in ignore and sub not in ignore:
                out[key] = _strip(item, ignore, sub)
        return out
    if isinstance(value, list):
        return [_strip(item, ignore, path) for item in value]
    return value


def _compare(old: Any, new: Any, path: str, out: List[Change]) -> None:
    """Append the differences between `old` and `new` to `out`. A missing
    key and an explicit null mean the same: nothing is known."""
    old = MISSING if old is None else old
    new = MISSING if new is None else new
    if isinstance(old, dict) and isinstance(new, dict):
        for key in sorted(set(old) | set(new), key=str):
            _compare(old.get(key, MISSING), new.get(key, MISSING),
                     f"{path}.{key}" if path else str(key), out)
    elif isinstance(old, list) and isinstance(new, list):
        _compare_lists(old, new, path, out)
    elif old is MISSING or new is MISSING:
        if old is not new:
            out.append(Change(path, old, new))
    elif _canon(old) != _canon(new):
        out.append(Change(path, old, new))


def _compare_lists(old: List[Any], new: List[Any], path: str, out: List[Change]) -> None:
    key = _identity_key(old, new)
    if key is not None:
        by_old = {str(x[key]): x for x in old}
        by_new = {str(x[key]): x for x in new}
        for name in sorted(set(by_old) | set(by_new)):
            _compare(by_old.get(name, MISSING), by_new.get(name, MISSING), f"{path}[{name}]", out)
        return
    canon_old = {_canon(x): x for x in old}
    canon_new = {_canon(x): x for x in new}
    added = [canon_new[c] for c in sorted(canon_new) if c not in canon_old]
    removed = [canon_old[c] for c in sorted(canon_old) if c not in canon_new]
    if added or removed:
        out.append(Change(path, added=added, removed=removed))


def _identity_key(old: List[Any], new: List[Any]) -> Optional[str]:
    """The field that names each item of a list of dicts, if one does: it
    must be a scalar on every item and unique within each snapshot."""
    items = old + new
    if not items or not all(isinstance(x, dict) for x in items):
        return None
    for key in IDENTITY_KEYS:
        if not all(isinstance(x.get(key), (str, int)) and not isinstance(x.get(key), bool)
                   for x in items):
            continue
        if all(len({str(x[key]) for x in side}) == len(side) for side in (old, new)):
            return key
    return None


def _canon(value: Any) -> str:
    """Canonical text for comparing values: dict order never matters, and
    1, 1.0 and true stay distinct."""
    return json.dumps(value, sort_keys=True, default=str)


# -- text ----------------------------------------------------------------------

def _fmt(value: Any) -> str:
    if value is MISSING or value is None:
        return "(none)"
    if isinstance(value, str) and value and value == value.strip():
        return value
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def _describe_node(topo: Topology, node_id: str) -> str:
    node = topo.nodes[node_id]
    if node.label == node.id:
        return f"{node.kind} {node.id}"
    return f"{node.kind} {node.label}  [{node.id}]"


def _describe_link(topo: Topology, lid: str) -> str:
    link = topo.links[lid]
    ends = [topo.nodes[e].label if e in topo.nodes else e for e in (link.source, link.target)]
    arrow = ARROW if link.directed else "↔"
    label = f" \"{link.label}\"" if link.label else ""
    return f"{link.kind} {ends[0]} {arrow} {ends[1]}{label}  [{link.id}]"


def _write(text: str) -> None:
    """Write to stdout, falling back to ASCII arrows on consoles that can't
    encode them (Windows with output redirected, for one)."""
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "ascii"
        sys.stdout.write(text.translate(_ASCII).encode(encoding, "replace").decode(encoding))


# -- CLI -----------------------------------------------------------------------

def add_arguments(parser) -> None:
    parser.add_argument("old", help="the earlier snapshot ('-' for stdin)")
    parser.add_argument("new", help="the later snapshot ('-' for stdin)")
    parser.add_argument("--format", choices=("text", "json"), default="text",
                        help="output format (default: text)")
    parser.add_argument("--ignore-attr", action="append", default=[], metavar="NAME",
                        help="ignore this attribute: a key at any depth (state) or a dotted path"
                             " (tags.LastSeen); repeatable. 'sources' and meta are always ignored")
    parser.add_argument("--summary", action="store_true", help="print counts only")


def run(args) -> int:
    try:
        old, new = read_topologies([args.old, args.new])
    except (OSError, ValueError) as e:
        print(f"topomapper: error: {e}", file=sys.stderr)
        return 2
    result = diff(old, new, (*DEFAULT_IGNORE, *args.ignore_attr))
    if args.format == "json":
        _write(json.dumps(result.to_dict(args.summary), indent=2, sort_keys=True) + "\n")
    else:
        _write(result.format_text(args.summary))
    return 0 if result.identical else 1
