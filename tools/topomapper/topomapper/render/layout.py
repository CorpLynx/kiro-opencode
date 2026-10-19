"""Geometry shared by the draw.io and SVG renderers.

The layout is computed bottom-up over the containment tree (`parent`):
each container arranges its children, then is sized to fit them plus a
title bar and padding, and is itself arranged as one block by its parent.
Containment therefore holds by construction, and siblings never overlap
because every arrangement packs blocks with gaps between them.

How a container arranges its children:

* **Generic (regions, accounts, the top level, subnets):** links
  between descendants are lifted to the children that contain them, so a
  TGW attachment to an instance's VPC pulls the TGW towards that VPC.
  Each connected group is drawn in layers by breadth-first distance from
  its most "outside" members (internet, then customer gateways and Direct
  Connect, then AWS gateways, then what sits in the VPCs), with a few
  barycenter sweeps to reduce crossings and an isotonic-regression pass to
  centre blocks over their neighbours. Unlinked children go in a grid.
* **Account:** the `iam` box (roles and users) is pinned to the top-right
  corner, with everything else arranged to its left, so IAM is always in
  the same place whatever else the account holds.
* **VPC:** gateways and endpoints in a row along the top, then the AZs as
  side-by-side columns. Subnets are stacked in their AZ, public above
  private, and aligned into rows across AZs so the VPC reads as a table.

Everything is iterated in sorted order and rounded to whole pixels, so
the same topology always produces the same coordinates.
"""

from __future__ import annotations

import ipaddress
import math
import re
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from ..model import AWS_ACCOUNT, AWS_AZ, AWS_IAM, AWS_SUBNET, AWS_VPC, Link, Topology
from . import labels, styles

# Node labels are 12px Helvetica; widths are estimated generously so text
# never runs into a neighbour.
CHAR_W = 6.9
BOLD_CHAR_W = 7.5
LINE_H = 15
EDGE_CHAR_W = 6.0
EDGE_LINE_H = 13

PAD = 16             # inside a container, left/right/bottom
GAP = 32             # between siblings
LAYER_GAP = {"TB": 72, "LR": 150}
PART_GAP = 40        # between the stacked parts of a VPC
MARGIN = 20
TITLE_H = 36
PARALLEL_SPREAD = 18
MIN_CONTAINER_W = 140
MAX_ROW = 1200      # a layer wider than this (and than it is tall) wraps
EDGE_ROW_KINDS = ("internet-gateway", "egress-only-internet-gateway", "vpn-gateway", "vpc-endpoint")

Point = Tuple[int, int]
Rect = Tuple[int, int, int, int]


@dataclass
class Box:
    """A drawn node in absolute coordinates. For a container, `header` is
    the title bar height; for a leaf, `icon` is the shape and the rest of
    the box is its label underneath."""
    id: str
    vclass: str
    x: int
    y: int
    w: int
    h: int
    container: bool
    lines: List[str]
    header: int = 0
    icon: Rect = (0, 0, 0, 0)
    parent: Optional[str] = None


@dataclass
class Route:
    """A link to draw: `points` run from the source shape's border, through
    any waypoint, to the target shape's border."""
    link: Link
    points: List[Point]
    waypoint: Optional[Point] = None
    # Where the port labels go: just past each end node's own label, as a
    # point and as a fraction of the route's length (draw.io's -1..1 scale).
    source_label: Tuple[float, float] = (0.0, 0.0)
    target_label: Tuple[float, float] = (0.0, 0.0)
    source_t: float = 0.15
    target_t: float = 0.15


@dataclass
class Layout:
    boxes: Dict[str, Box]
    order: List[str]                 # parents before children
    routes: List[Route]
    skipped: List[str] = field(default_factory=list)   # link IDs not drawn
    width: int = 0
    height: int = 0
    title: str = ""
    direction: str = "TB"


def natural(text: str) -> tuple:
    """Sort key that puts app-2 before app-10."""
    return tuple((0, int(p), "") if p.isdigit() else (1, 0, p)
                 for p in re.split(r"(\d+)", text.lower()) if p)


def text_width(text: str, bold: bool = False) -> int:
    return int(math.ceil(len(text) * (BOLD_CHAR_W if bold else CHAR_W)))


def edge_text_width(text: str) -> int:
    return int(math.ceil(len(text) * EDGE_CHAR_W)) + 6


def layout(topo: Topology, direction: str = "TB", title: str = "") -> Layout:
    """Lay out every node of `topo`; see the module docstring."""
    if direction not in LAYER_GAP:
        raise ValueError(f"unknown direction {direction!r} (expected TB or LR)")
    return _Engine(topo, direction, title).run()


class _Engine:
    def __init__(self, topo: Topology, direction: str, title: str):
        self.topo = topo
        self.direction = direction
        self.title = title
        self.parent = styles.effective_parents(topo)
        self.kids: Dict[Optional[str], List[str]] = {}
        for nid in sorted(topo.nodes):
            self.kids.setdefault(self.parent[nid], []).append(nid)
        self.vclass = styles.classify(topo, self.parent)
        self.lines = {nid: labels.node_lines(topo.nodes[nid]) for nid in topo.nodes}
        self.size: Dict[str, Tuple[int, int]] = {}
        self.rel: Dict[str, Tuple[int, int]] = {}
        self.icon: Dict[str, Rect] = {}
        self.header: Dict[str, int] = {}
        self._sort_keys: Dict[str, tuple] = {}
        self.uplink: Dict[str, int] = {}
        self.block_edges = self._lift_links()

    # -- structure -----------------------------------------------------------

    def is_container(self, nid: str) -> bool:
        return styles.is_container_class(self.vclass[nid])

    def kind(self, nid: str) -> str:
        return self.topo.nodes[nid].kind

    def chain(self, nid: str) -> List[str]:
        """nid and its ancestors, nearest first."""
        out = [nid]
        while self.parent[out[-1]] is not None:
            out.append(self.parent[out[-1]])  # type: ignore[arg-type]
        return out

    def drawable(self, link: Link) -> bool:
        """Links to an ancestor (a VGW attached to the VPC it sits in) are
        already said by containment and can't be drawn sensibly."""
        if link.source not in self.topo.nodes or link.target not in self.topo.nodes:
            return False
        if link.source == link.target:
            return False
        return (link.source not in self.chain(link.target)
                and link.target not in self.chain(link.source))

    def _lift_links(self) -> Dict[Optional[str], List[Tuple[str, str]]]:
        """Each link becomes one edge between the two children of its ends'
        lowest common container (None for the top level)."""
        out: Dict[Optional[str], List[Tuple[str, str]]] = {}
        for link in sorted(self.topo.links.values(), key=lambda l: l.id):
            if not self.drawable(link):
                continue
            a = list(reversed(self.chain(link.source)))
            b = list(reversed(self.chain(link.target)))
            i = 0
            while i < min(len(a), len(b)) and a[i] == b[i]:
                i += 1
            lca = a[i - 1] if i else None
            out.setdefault(lca, []).append((a[i], b[i]))
            for inside, other in ((a, link.target), (b, link.source)):
                for block in inside[i + 1:]:
                    self.uplink[block] = min(self.uplink.get(block, 99), self.tier(other))
        return out

    def tier(self, nid: str) -> int:
        return styles.tier(self.vclass[nid])

    def anchor_tier(self, nid: str) -> int:
        """Tier used to choose a group's top layer. A link leaving the
        container counts too: a gateway whose link goes to the internet
        outside its container is drawn above what sits behind it."""
        return min(self.tier(nid), self.uplink.get(nid, 99) + 1)

    def sort_key(self, nid: str) -> tuple:
        key = self._sort_keys.get(nid)
        if key is None:
            node = self.topo.nodes[nid]
            key = self._sort_keys[nid] = (self.tier(nid), self.vclass[nid], natural(node.label), nid)
        return key

    # -- sizing ----------------------------------------------------------------

    def title_lines(self, nid: str) -> List[str]:
        return self.lines[nid][:2]

    def measure(self, nid: str) -> None:
        if not self.is_container(nid):
            self._measure_leaf(nid)
            return
        if self.kind(nid) == AWS_AZ:
            self._arrange_azs([nid])
            return
        kids = self.kids.get(nid, [])
        if self.kind(nid) == AWS_VPC:
            pos, w, h = self._arrange_vpc(nid, kids)
        else:
            for k in kids:
                self.measure(k)
            if self.kind(nid) == AWS_ACCOUNT and any(
                    self.kind(k) == AWS_IAM and self.is_container(k) for k in kids):
                pos, w, h = self._arrange_account(nid, kids)
            else:
                pos, w, h = self._arrange_generic(nid, kids)
        self._wrap(nid, pos, w, h)

    def _measure_leaf(self, nid: str) -> None:
        shape = styles.shape(self.vclass[nid])
        lines = self.lines[nid]
        text_w = max(text_width(l, i == 0) for i, l in enumerate(lines))
        if shape.boxed:
            w = max(shape.w, text_w + 16)
            h = max(shape.h, len(lines) * LINE_H + 12)
            self.icon[nid] = (0, 0, w, h)
        else:
            w = max(shape.w, text_w)
            if (w - shape.w) % 2:
                w += 1
            h = shape.h + 6 + len(lines) * LINE_H
            self.icon[nid] = ((w - shape.w) // 2, 0, shape.w, shape.h)
        self.size[nid] = (w, h)

    def _header(self, nid: str) -> Tuple[int, int]:
        """(title bar height, minimum width for the title)."""
        lines = self.title_lines(nid)
        header = max(32, 12 + LINE_H * len(lines))
        min_w = 30 + max(text_width(l) for l in lines) + PAD
        if self.vclass[nid] in ("iam", "bedrock"):
            min_w += 36                      # room for the logo beside the title
        return header, max(MIN_CONTAINER_W, min_w)

    def _wrap(self, nid: str, pos: Dict[str, Point], w: int, h: int) -> None:
        """Size a container around content laid out from (0, 0)."""
        header, min_w = self._header(nid)
        self.header[nid] = header
        inner_w = max(w, min_w - 2 * PAD)
        dx = PAD + (inner_w - w) // 2
        for k, (x, y) in pos.items():
            self.rel[k] = (x + dx, y + header + 8)
        self.size[nid] = (inner_w + 2 * PAD, header + 8 + max(h, 24) + PAD)

    # -- generic arrangement -------------------------------------------------------

    def _arrange_generic(self, container: Optional[str], kids: Sequence[str]
                         ) -> Tuple[Dict[str, Point], int, int]:
        members = set(kids)
        adj: Dict[str, Set[str]] = {k: set() for k in kids}
        for a, b in self.block_edges.get(container, []):
            if a in members and b in members and a != b:
                adj[a].add(b)
                adj[b].add(a)
        comps = self._components(kids, adj)
        singles = [c[0] for c in comps if len(c) == 1]
        edge = [n for n in singles if self.anchor_tier(n) < styles.CONTAINER_TIER]
        loose = [n for n in singles if n not in edge]
        multi = [c for c in comps if len(c) > 1]
        parts = []
        if multi:
            # Unlinked edge things (a DX connection whose only link leaves
            # the region) join the top row of the main group.
            laid = [self._layered(c, adj, edge if i == 0 else []) for i, c in enumerate(multi)]
            parts.append(self._shelf(laid))
        elif edge:
            parts.append(self._grid(edge))
        if loose:
            parts.append(self._grid(loose))
        return self._stack(parts, GAP)

    def _stack(self, parts: List[Tuple[Dict[str, Point], int, int]], gap: int
               ) -> Tuple[Dict[str, Point], int, int]:
        """Parts one above the other, centred on the widest."""
        if not parts:
            return {}, 0, 0
        width = max(w for _, w, _ in parts)
        pos: Dict[str, Point] = {}
        y = 0
        for ppos, w, h in parts:
            dx = (width - w) // 2
            for k, (px, py) in ppos.items():
                pos[k] = (px + dx, py + y)
            y += h + gap
        return pos, width, y - gap

    def _components(self, kids: Sequence[str], adj: Dict[str, Set[str]]) -> List[List[str]]:
        seen: Set[str] = set()
        comps = []
        for k in sorted(kids, key=self.sort_key):
            if k in seen:
                continue
            comp, queue = [], deque([k])
            seen.add(k)
            while queue:
                u = queue.popleft()
                comp.append(u)
                for v in sorted(adj[u]):
                    if v not in seen:
                        seen.add(v)
                        queue.append(v)
            comps.append(sorted(comp, key=self.sort_key))
        comps.sort(key=lambda c: (min(self.tier(n) for n in c), -len(c), self.sort_key(c[0])))
        return comps

    def _main_cross(self, nid: str) -> Tuple[int, int]:
        w, h = self.size[nid]
        return (h, w) if self.direction == "TB" else (w, h)

    def _layered(self, comp: List[str], adj: Dict[str, Set[str]], extra_top: Sequence[str] = ()
                 ) -> Tuple[Dict[str, Point], int, int]:
        top = min(self.anchor_tier(n) for n in comp)
        layer: Dict[str, int] = {}
        queue = deque()
        for n in comp:
            if self.anchor_tier(n) == top:
                layer[n] = 0
                queue.append(n)
        # 0-1 BFS: linked containers (two peered VPCs, a VPN between
        # accounts) sit side by side rather than one below the other.
        while queue:
            u = queue.popleft()
            for v in sorted(adj[u], key=self.sort_key):
                step = 1 if self._steps_down(u, v) else 0
                if v not in layer or layer[u] + step < layer[v]:
                    layer[v] = layer[u] + step
                    if step:
                        queue.append(v)
                    else:
                        queue.appendleft(v)
        comp = sorted(list(comp) + list(extra_top), key=self.sort_key)
        for n in extra_top:
            layer[n] = 0
        layers: List[List[str]] = [[] for _ in range(max(layer.values()) + 1)]
        for n in comp:
            layers[layer[n]].append(n)
        _reduce_crossings(layers, adj, layer)
        layers = self._wrap_layers(layers)
        layer = {n: i for i, row in enumerate(layers) for n in row}

        cross = {n: self._main_cross(n)[1] for n in comp}
        center = _assign_cross(layers, adj, layer, cross, GAP)
        left = min(center[n] - cross[n] / 2 for n in comp)

        pos: Dict[str, Point] = {}
        main_at = 0
        thick_total = 0
        for row in layers:
            thick = max(self._main_cross(n)[0] for n in row)
            for n in row:
                c = int(round(center[n] - cross[n] / 2 - left))
                if self.direction == "TB":
                    pos[n] = (c, main_at)
                else:
                    pos[n] = (main_at + (thick - self.size[n][0]) // 2, c)
            thick_total = main_at + thick
            main_at += thick + LAYER_GAP[self.direction]
        extent = max(pos[n][0 if self.direction == "TB" else 1] + cross[n] for n in comp)
        return (pos, extent, thick_total) if self.direction == "TB" else (pos, thick_total, extent)

    def _wrap_layers(self, layers: List[List[str]]) -> List[List[str]]:
        """Split a layer that would be far wider than it is tall (40 VPCs
        on one transit gateway, a dozen peered VPCs) into several rows,
        keeping the order crossing reduction chose."""
        out: List[List[str]] = []
        for row in layers:
            if len(row) <= 3:
                out.append(row)
                continue
            cross = [self._main_cross(n)[1] for n in row]
            area = sum(self._main_cross(n)[0] * c for n, c in zip(row, cross))
            limit = max(MAX_ROW, max(cross), int(math.sqrt(area) * 2.5))
            chunk: List[str] = []
            used = 0
            for n, c in zip(row, cross):
                if chunk and used + c > limit:
                    out.append(chunk)
                    chunk, used = [], 0
                chunk.append(n)
                used += c + GAP
            out.append(chunk)
        return out

    def _steps_down(self, u: str, v: str) -> bool:
        """Whether a link puts `v` one layer below `u`. Not between a
        container and a peer of container rank or lower (two peered VPCs,
        a VPN between accounts): those read better side by side."""
        if not (self.is_container(u) or self.is_container(v)):
            return True
        return min(self.tier(u), self.tier(v)) < styles.CONTAINER_TIER

    def _shelf(self, parts: List[Tuple[Dict[str, Point], int, int]]
               ) -> Tuple[Dict[str, Point], int, int]:
        """Pack laid-out components in rows (TB) or columns (LR), wrapping
        so a topology with many small islands doesn't become a long strip."""
        pos: Dict[str, Point] = {}
        if not parts:
            return pos, 0, 0
        tb = self.direction == "TB"
        area = sum(w * h for _, w, h in parts)
        widest = max((w if tb else h) for _, w, h in parts)
        limit = max(widest, int(math.sqrt(area) * 2))
        along = across = line_thick = 0
        total_along = 0
        for ppos, w, h in parts:
            span, thick = (w, h) if tb else (h, w)
            if along and along + span > limit:
                across += line_thick + GAP
                along = line_thick = 0
            for k, (x, y) in ppos.items():
                pos[k] = (x + along, y + across) if tb else (x + across, y + along)
            along += span + GAP
            total_along = max(total_along, along - GAP)
            line_thick = max(line_thick, thick)
        total_across = across + line_thick
        return (pos, total_along, total_across) if tb else (pos, total_across, total_along)

    def _grid(self, items: Sequence[str]) -> Tuple[Dict[str, Point], int, int]:
        """A tidy table for blocks with no links between them."""
        items = sorted(items, key=self.sort_key)
        n = len(items)
        if n <= 4:
            cols = n
        else:
            avg_w = sum(self.size[i][0] for i in items) / n
            avg_h = sum(self.size[i][1] for i in items) / n
            cols = max(1, min(n, int(round(math.sqrt(n * 2.0 * avg_h / max(avg_w, 1))))))
        rows = (n + cols - 1) // cols
        col_w = [0] * cols
        row_h = [0] * rows
        for i, item in enumerate(items):
            w, h = self.size[item]
            col_w[i % cols] = max(col_w[i % cols], w)
            row_h[i // cols] = max(row_h[i // cols], h)
        col_x = [sum(col_w[:c]) + c * GAP for c in range(cols)]
        row_y = [sum(row_h[:r]) + r * GAP for r in range(rows)]
        pos = {}
        for i, item in enumerate(items):
            c, r = i % cols, i // cols
            pos[item] = (col_x[c] + (col_w[c] - self.size[item][0]) // 2, row_y[r])
        return pos, col_x[-1] + col_w[-1], row_y[-1] + row_h[-1]

    # -- AWS conventions -------------------------------------------------------

    def _arrange_account(self, account: str, kids: Sequence[str]
                         ) -> Tuple[Dict[str, Point], int, int]:
        """Regions and the rest to the left, the IAM box in the top-right corner."""
        boxes = [k for k in kids if self.kind(k) == AWS_IAM and self.is_container(k)]
        rest = [k for k in kids if k not in boxes]
        iam = boxes[0]
        iam_w, iam_h = self.size[iam]
        if not rest:
            return {iam: (0, 0)}, iam_w, iam_h
        pos, w, h = self._arrange_generic(account, rest)
        pos = dict(pos)
        pos[iam] = (w + GAP, 0)
        return pos, w + GAP + iam_w, max(h, iam_h)

    def _arrange_vpc(self, vpc: str, kids: Sequence[str]) -> Tuple[Dict[str, Point], int, int]:
        edge = [k for k in kids if not self.is_container(k) and self.kind(k) in EDGE_ROW_KINDS]
        azs = [k for k in kids if self.kind(k) == AWS_AZ and self.is_container(k)]
        rest = [k for k in kids if k not in edge and k not in azs]
        for k in edge + rest:
            self.measure(k)

        parts: List[Tuple[Dict[str, Point], int, int]] = []
        if edge:
            edge.sort(key=lambda k: (EDGE_ROW_KINDS.index(self.kind(k)), self.sort_key(k)))
            row: Dict[str, Point] = {}
            x = 0
            for k in edge:
                row[k] = (x, 0)
                x += self.size[k][0] + GAP
            parts.append((row, x - GAP, max(self.size[k][1] for k in edge)))
        if azs:
            parts.append(self._arrange_azs(sorted(azs, key=lambda a: (natural(self.topo.nodes[a].label), a))))
        if rest:
            parts.append(self._arrange_generic(vpc, rest))
        return self._stack(parts, PART_GAP)

    def _arrange_azs(self, azs: List[str]) -> Tuple[Dict[str, Point], int, int]:
        """AZs side by side; inside each, subnets in bands (public, then
        private), with row heights shared across AZs so subnets line up."""
        bands = ("subnet-public", "subnet-private")
        per: Dict[str, Dict[str, List[str]]] = {}
        others: Dict[str, Tuple[Dict[str, Point], int, int]] = {}
        for az in azs:
            kids = self.kids.get(az, [])
            for k in kids:
                self.measure(k)
            subnets = [k for k in kids if self.kind(k) == AWS_SUBNET and self.is_container(k)]
            per[az] = {b: sorted((s for s in subnets if self.vclass[s] == b), key=self._subnet_key)
                       for b in bands}
            rest = [k for k in kids if k not in subnets]
            others[az] = self._arrange_generic(az, rest) if rest else ({}, 0, 0)

        rows: List[Tuple[str, int, int]] = []     # (band, index, height)
        for band in bands:
            for j in range(max(len(per[az][band]) for az in azs)):
                rows.append((band, j, max(self.size[per[az][band][j]][1] for az in azs
                                          if j < len(per[az][band]))))
        rows_h = sum(h for _, _, h in rows) + GAP * max(len(rows) - 1, 0)
        others_h = max(h for _, _, h in others.values())

        pos: Dict[str, Point] = {}
        x = 0
        heights = {}
        for az in azs:
            header, min_w = self._header(az)
            self.header[az] = header
            subs = [s for b in bands for s in per[az][b]]
            inner = max([self.size[s][0] for s in subs] + [others[az][1], min_w - 2 * PAD])
            y = header + 8
            for band, j, h in rows:
                if j < len(per[az][band]):
                    s = per[az][band][j]
                    self.size[s] = (inner, h)
                    self.rel[s] = (PAD, y)
                y += h + GAP
            y = header + 8 + rows_h + (GAP if rows and others_h else 0)
            opos, ow, _ = others[az]
            for k, (ox, oy) in opos.items():
                self.rel[k] = (PAD + (inner - ow) // 2 + ox, y + oy)
            heights[az] = header + 8 + max(rows_h + (GAP if rows and others_h else 0) + others_h, 24) + PAD
            self.size[az] = (inner + 2 * PAD, 0)
            pos[az] = (x, 0)
            x += inner + 2 * PAD + GAP
        tallest = max(heights.values())
        for az in azs:
            self.size[az] = (self.size[az][0], tallest)
        return pos, x - GAP, tallest

    def _subnet_key(self, sid: str) -> tuple:
        cidr = str(self.topo.nodes[sid].attrs.get("cidr") or "")
        try:
            net = ipaddress.ip_network(cidr, strict=False)
            return (0, net.version, int(net.network_address), net.prefixlen, sid)
        except ValueError:
            return (1, 0, 0, 0, sid)

    # -- assembly -----------------------------------------------------------------

    def run(self) -> Layout:
        roots = self.kids.get(None, [])
        for nid in roots:
            self.measure(nid)
        pos, w, h = self._arrange_generic(None, roots) if roots else ({}, 0, 0)
        top = MARGIN + (TITLE_H if self.title else 0)
        boxes: Dict[str, Box] = {}
        order: List[str] = []

        def place(nid: str, x: int, y: int) -> None:
            w_, h_ = self.size[nid]
            container = self.is_container(nid)
            ix, iy, iw, ih = self.icon.get(nid, (0, 0, w_, h_))
            boxes[nid] = Box(nid, self.vclass[nid], x, y, w_, h_, container, self.lines[nid],
                             self.header.get(nid, 0), (x + ix, y + iy, iw, ih), self.parent[nid])
            order.append(nid)
            for k in self.kids.get(nid, []):
                rx, ry = self.rel[k]
                place(k, x + rx, y + ry)

        for nid in roots:
            px, py = pos[nid]
            place(nid, MARGIN + px, top + py)
        width = MARGIN * 2 + max(w, text_width(self.title, True) if self.title else 0)
        height = top + h + MARGIN
        out = Layout(boxes, order, [], [], width, height, self.title, self.direction)
        out.routes, out.skipped = self._routes(boxes)
        return out

    def _routes(self, boxes: Dict[str, Box]) -> Tuple[List[Route], List[str]]:
        groups: Dict[Tuple[str, str], List[Link]] = {}
        skipped = []
        for link in sorted(self.topo.links.values(), key=lambda l: l.id):
            if not self.drawable(link):
                skipped.append(link.id)
                continue
            groups.setdefault(tuple(sorted((link.source, link.target))), []).append(link)
        routes = []
        for key in sorted(groups):
            links = groups[key]
            n = len(links)
            for i, link in enumerate(links):
                ra, rb = boxes[link.source].icon, boxes[link.target].icon
                ca, cb = _center(ra), _center(rb)
                offset = (i - (n - 1) / 2) * PARALLEL_SPREAD
                waypoint = None
                if offset:
                    dx, dy = cb[0] - ca[0], cb[1] - ca[1]
                    length = math.hypot(dx, dy) or 1.0
                    mid = ((ca[0] + cb[0]) / 2 - dy / length * offset,
                           (ca[1] + cb[1]) / 2 + dx / length * offset)
                    waypoint = (int(round(mid[0])), int(round(mid[1])))
                    points = [_clip(ra, waypoint), waypoint, _clip(rb, waypoint)]
                else:
                    points = [_clip(ra, _round(cb)), _clip(rb, _round(ca))]
                route = Route(link, points, waypoint)
                _place_end_labels(route, boxes[link.source], boxes[link.target])
                routes.append(route)
        return routes, skipped


# -- layer helpers (pure functions) -------------------------------------------------

def _reduce_crossings(layers: List[List[str]], adj: Dict[str, Set[str]],
                      layer: Dict[str, int], sweeps: int = 4) -> None:
    """Barycenter heuristic: reorder each layer by the mean relative position
    of its neighbours in the layers already fixed, alternately downwards
    and upwards. Stable sorting with the current index as a tie-break keeps
    it deterministic."""
    index: Dict[str, float] = {}

    def refresh(i: int) -> None:
        for k, n in enumerate(layers[i]):
            index[n] = (k + 0.5) / len(layers[i])

    for i in range(len(layers)):
        refresh(i)
    for sweep in range(sweeps):
        down = sweep % 2 == 0
        span = range(1, len(layers)) if down else range(len(layers) - 2, -1, -1)
        for i in span:
            def bary(n: str) -> float:
                nb = sorted(index[v] for v in adj[n] if (layer[v] < i if down else layer[v] > i))
                return sum(nb) / len(nb) if nb else index[n]
            layers[i] = sorted(layers[i], key=lambda n: (bary(n), index[n]))
            refresh(i)


def _assign_cross(layers: List[List[str]], adj: Dict[str, Set[str]], layer: Dict[str, int],
                  size: Dict[str, int], gap: int, rounds: int = 2) -> Dict[str, float]:
    """Cross-axis centres: each block as close as possible to the mean of
    its neighbours in the previous layers (then the next ones), keeping the
    order and gaps within its layer."""
    center: Dict[str, float] = {}
    for row in layers:
        _place_row(row, {n: 0.0 for n in row}, size, gap, center, pack=True)
    for _ in range(rounds):
        for down in (True, False):
            span = range(1, len(layers)) if down else range(len(layers) - 2, -1, -1)
            for i in span:
                want = {}
                for n in layers[i]:
                    nb = sorted(center[v] for v in adj[n] if (layer[v] < i if down else layer[v] > i))
                    want[n] = sum(nb) / len(nb) if nb else center[n]
                _place_row(layers[i], want, size, gap, center)
    return center


def _place_row(row: List[str], want: Dict[str, float], size: Dict[str, int], gap: int,
               center: Dict[str, float], pack: bool = False) -> None:
    """Pool-adjacent-violators: the least-squares placement of `row` near
    `want` subject to c[i+1] - c[i] >= (w[i] + w[i+1]) / 2 + gap."""
    offsets = [0.0]
    for a, b in zip(row, row[1:]):
        offsets.append(offsets[-1] + (size[a] + size[b]) / 2 + gap)
    if pack:
        shift = offsets[-1] / 2
        for n, o in zip(row, offsets):
            center[n] = o - shift
        return
    targets = [want[n] - o for n, o in zip(row, offsets)]
    blocks: List[List[float]] = []          # [mean, count]
    for t in targets:
        blocks.append([t, 1])
        while len(blocks) > 1 and blocks[-2][0] > blocks[-1][0]:
            m2, c2 = blocks.pop()
            m1, c1 = blocks.pop()
            blocks.append([(m1 * c1 + m2 * c2) / (c1 + c2), c1 + c2])
    values: List[float] = []
    for mean, count in blocks:
        values.extend([mean] * int(count))
    for n, v, o in zip(row, values, offsets):
        center[n] = v + o


def _center(r: Rect) -> Tuple[float, float]:
    return (r[0] + r[2] / 2, r[1] + r[3] / 2)


def _round(p: Tuple[float, float]) -> Point:
    return (int(round(p[0])), int(round(p[1])))


def _place_end_labels(route: Route, source: Box, target: Box) -> None:
    pts = route.points
    total = sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:])) or 1.0
    for end, box, a, b in (("source", source, pts[0], pts[1]), ("target", target, pts[-1], pts[-2])):
        seg = math.hypot(b[0] - a[0], b[1] - a[1]) or 1.0
        rect = (box.x, box.y, box.w, box.h) if not box.container else box.icon
        dist = min(_exit(a, b, rect) + 14, seg * 0.4)
        point = (a[0] + (b[0] - a[0]) * dist / seg, a[1] + (b[1] - a[1]) * dist / seg)
        setattr(route, f"{end}_label", (round(point[0], 1), round(point[1], 1)))
        setattr(route, f"{end}_t", round(min(dist / total, 0.45), 3))


def _exit(p: Point, q: Point, r: Rect) -> float:
    """How far from `p` towards `q` the segment is still inside `r`."""
    dx, dy = q[0] - p[0], q[1] - p[1]
    length = math.hypot(dx, dy)
    if not length or not (r[0] <= p[0] <= r[0] + r[2] and r[1] <= p[1] <= r[1] + r[3]):
        return 0.0
    ts = []
    if dx:
        ts.append(((r[0] + r[2] if dx > 0 else r[0]) - p[0]) / dx)
    if dy:
        ts.append(((r[1] + r[3] if dy > 0 else r[1]) - p[1]) / dy)
    return max(0.0, min(ts)) * length


def _clip(r: Rect, toward: Tuple[float, float]) -> Point:
    """Where the ray from the centre of `r` towards `toward` leaves `r`."""
    cx, cy = _center(r)
    dx, dy = toward[0] - cx, toward[1] - cy
    if dx == 0 and dy == 0:
        return _round((cx, cy))
    tx = (r[2] / 2) / abs(dx) if dx else math.inf
    ty = (r[3] / 2) / abs(dy) if dy else math.inf
    t = min(tx, ty, 1.0)
    return _round((cx + dx * t, cy + dy * t))

