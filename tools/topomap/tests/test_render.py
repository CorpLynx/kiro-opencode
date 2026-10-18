import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from topomap.model import Topology, node_id, union  # noqa: E402
from topomap.render import infer_format, render  # noqa: E402
from topomap.render.layout import layout  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures", "render")
REGEN = os.environ.get("TOPOMAP_REGEN_FIXTURES") == "1"
FORMATS = ("drawio", "svg", "mermaid", "dot")
EXT = {"drawio": "drawio", "svg": "svg", "mermaid": "mmd", "dot": "dot"}

ACCT = "123456789012"
REGION = f"aws:region:{ACCT}:us-east-1"
SPECIAL = 'R&D <lab> "core" \'x\''


def aws_topology() -> Topology:
    t = Topology({"collector": "test"})
    acct = t.add_node(f"aws:account:{ACCT}", "account", "example-prod", account_id=ACCT).id
    t.add_node(REGION, "region", "us-east-1", acct, region="us-east-1")
    for v, (name, cidr) in enumerate((("prod", "10.0.0.0/16"), ("shared", "10.1.0.0/16"))):
        vpc = t.add_node(f"aws:vpc:vpc-0{v}", "vpc", f"{name} (vpc-0{v})", REGION, cidrs=[cidr]).id
        for a, az in enumerate(("us-east-1a", "us-east-1b")):
            azid = t.add_node(f"aws:az:vpc-0{v}:{az}", "az", az, vpc).id
            for p, public in enumerate((True, False)):
                sid = f"subnet-{v}{a}{p}"
                sub = t.add_node(f"aws:subnet:{sid}", "subnet",
                                 f"{name}-{'public' if public else 'private'}-{az[-1]} ({sid})",
                                 azid, cidr=f"10.{v}.{a * 2 + p}.0/24", public=public, az=az).id
                if public and a == 0:
                    t.add_node(f"aws:nat-gateway:nat-0{v}", "nat-gateway", f"nat-0{v}", sub,
                               nat_gateway_id=f"nat-0{v}", public_ips=[f"203.0.113.{20 + v}"])
                if not public:
                    for i in range(2):
                        iid = f"i-0{v}{a}{i}"
                        t.add_node(f"aws:instance:{iid}", "instance", f"app-{v}{a}{i} ({iid})", sub,
                                   instance_id=iid, private_ips=[f"10.{v}.{a * 2 + p}.{10 + i}"],
                                   instance_type="t3.micro")
        t.add_node(f"aws:internet-gateway:igw-0{v}", "internet-gateway", f"igw-0{v}", vpc,
                   gateway_id=f"igw-0{v}")
    t.add_node("aws:vpc-endpoint:vpce-01", "vpc-endpoint", "vpce-01", "aws:vpc:vpc-01",
               endpoint_id="vpce-01", service="com.amazonaws.us-east-1.s3", endpoint_type="Gateway")
    t.add_node("aws:vpn-gateway:vgw-01", "vpn-gateway", "vgw-01", "aws:vpc:vpc-01",
               vgw_id="vgw-01", asn=64513)
    t.add_link("vgw-attachment", "aws:vpn-gateway:vgw-01", "aws:vpc:vpc-01", state="attached")
    tgw = t.add_node("aws:transit-gateway:tgw-01", "transit-gateway", "core-tgw (tgw-01)", REGION,
                     tgw_id="tgw-01", asn=64512).id
    for v in range(2):
        t.add_link("tgw-attachment", f"aws:vpc:vpc-0{v}", tgw, key=f"tgw-attach-0{v}",
                   attachment_id=f"tgw-attach-0{v}", state="available", resource_type="vpc")
    cgw = t.add_node("aws:customer-gateway:cgw-01", "customer-gateway", "hq-cgw (cgw-01)", REGION,
                     cgw_id="cgw-01", ip="203.0.113.10", asn=65010).id
    t.add_link("vpn", cgw, tgw, key="vpn-01", vpn_id="vpn-01", state="available", routing="bgp",
               tunnels=[{"outside_ip": "198.51.100.1", "inside_cidr": "169.254.10.0/30",
                         "status": "UP"},
                        {"outside_ip": "198.51.100.2", "inside_cidr": "169.254.11.0/30",
                         "status": "DOWN"}])
    dxcon = t.add_node("aws:dx-connection:dxcon-01", "dx-connection", "dxcon-01", REGION,
                       connection_id="dxcon-01", location="EqDC2", bandwidth="1Gbps").id
    dxgw = t.add_node("aws:dx-gateway:dxgw-01", "dx-gateway", "dxgw-01", acct,
                      dxgw_id="dxgw-01", asn=64600).id
    t.add_link("dx-vif", dxcon, dxgw, directed=True, key="dxvif-01", vif_id="dxvif-01",
               vif_type="transit", vlan=101, customer_address="192.168.100.1/30", state="available")
    t.add_link("dxgw-association", dxgw, tgw, association_id="dxgw-assoc-01", state="associated")
    t.add_link("vpc-peering", "aws:vpc:vpc-00", "aws:vpc:vpc-01", key="pcx-01",
               peering_id="pcx-01", state="active")
    t.add_node("aws:vpc:vpc-0ext", "vpc", "partner (vpc-0ext)", cidrs=["172.16.0.0/16"],
               account_id="111122223333", external=True)
    t.add_link("vpc-peering", "aws:vpc:vpc-00", "aws:vpc:vpc-0ext", key="pcx-02",
               peering_id="pcx-02", state="pending-acceptance")
    return t


def network_topology() -> Topology:
    t = Topology({"collector": "test"})
    site = t.add_node(node_id("net", "site", "hq"), "site", "HQ").id

    def dev(name, role, mgmt, model, parent=site):
        return t.add_node(f"net:device:{name}", "device", name, parent, role=role,
                          mgmt_ip=mgmt, model=model).id

    fw = dev("fw1", "firewall", "10.255.0.1", "ASA5516")
    r1 = dev("rtr1", "router", "10.255.0.2", "ISR4331")
    r2 = dev("rtr2", "router", "10.255.0.3", "ISR4331")
    c1 = dev("core1", "switch", "10.255.0.11", "C9500")
    c2 = dev("core2", "switch", "10.255.0.12", "C9500")
    a1 = dev("acc1", "switch", "10.255.0.21", "C9300")
    a2 = dev("acc2", "switch", "10.255.0.22", "C9300")
    ap = dev("ap1", "wireless", "10.255.1.10", "AIR-AP2802I")
    ph = dev("phone1", "phone", "", "CP-8845")
    sv = dev("srv1", "server", "10.20.0.10", "")
    odd = dev("lab1", "unknown", "10.255.0.99", SPECIAL)
    t.nodes[odd].label = SPECIAL
    inet = t.add_node("ext:external:internet", "external", "Internet").id

    def cable(a, ap_, b, bp, **kw):
        t.add_link("physical", a, b, source_port=ap_, target_port=bp, protocols=["cdp"], **kw)

    cable(fw, "GigabitEthernet0/1", r1, "GigabitEthernet0/0/0")
    cable(fw, "GigabitEthernet0/2", r2, "GigabitEthernet0/0/0")
    for r in (r1, r2):
        cable(r, "GigabitEthernet0/0/1", c1, f"TenGigabitEthernet1/0/{1 if r == r1 else 2}")
        cable(r, "GigabitEthernet0/0/2", c2, f"TenGigabitEthernet1/0/{1 if r == r1 else 2}")
    cable(c1, "TenGigabitEthernet1/0/10", c2, "TenGigabitEthernet1/0/10")
    cable(c1, "TenGigabitEthernet1/0/11", c2, "TenGigabitEthernet1/0/11")
    for a in (a1, a2):
        cable(c1, f"TenGigabitEthernet1/0/2{a[-1]}", a, "TenGigabitEthernet1/1/1")
        cable(c2, f"TenGigabitEthernet1/0/2{a[-1]}", a, "TenGigabitEthernet1/1/2")
    cable(a1, "GigabitEthernet1/0/1", ap, "GigabitEthernet0")
    cable(a1, "GigabitEthernet1/0/2", ph, "Port 1")
    cable(a2, "GigabitEthernet1/0/1", sv, "eth0")
    cable(a2, "GigabitEthernet1/0/48", odd, "eth0", subnets=[SPECIAL])
    t.add_link("physical", inet, fw, target_port="GigabitEthernet0/0")
    t.add_link("l3", r1, r2, source_port="Tunnel0", target_port="Tunnel0", label="10.0.255.0/30",
               subnet="10.0.255.0/30", addresses={r1: "10.0.255.1", r2: "10.0.255.2"})
    seg = t.add_node("net:segment:10.10.0.0/24", "segment", "10.10.0.0/24", subnet="10.10.0.0/24").id
    for d, ip in ((fw, "10.10.0.1"), (r1, "10.10.0.2"), (r2, "10.10.0.3")):
        t.add_link("segment-member", d, seg, source_port="Vlan10", address=ip)
    return t


def hybrid_topology() -> Topology:
    t = union([aws_topology(), network_topology()])
    t.meta = {"collector": "test"}
    t.add_link("stitch", "net:device:rtr1", "aws:customer-gateway:cgw-01", rule="cgw-ip",
               address="203.0.113.10")
    return t


def big_topology(n_sites: int = 12, per_site: int = 24) -> Topology:
    t = Topology()
    for s in range(n_sites):
        site = t.add_node(f"net:site:s{s}", "site", f"site{s}").id
        core = t.add_node(f"net:device:s{s}-core", "device", f"s{s}-core", site, role="switch").id
        rtr = t.add_node(f"net:device:s{s}-rtr", "device", f"s{s}-rtr", site, role="router").id
        t.add_link("physical", rtr, core, source_port="Gi0/1", target_port="Te1/1")
        for i in range(per_site - 2):
            acc = t.add_node(f"net:device:s{s}-acc{i}", "device", f"s{s}-acc{i}", site,
                             role="switch" if i % 3 == 0 else "phone").id
            t.add_link("physical", core, acc, source_port=f"Te1/{i + 2}", target_port="Gi0/1")
        if s:
            t.add_link("l3", rtr, f"net:device:s{s - 1}-rtr", subnet=f"10.{s}.0.0/30")
    return t


def parse(text: str) -> ET.Element:
    return ET.fromstring(text.encode("utf-8"))


def drawio_cells(root: ET.Element):
    """{id: (style, parent, value, element)} for every cell, unwrapping UserObject."""
    cells = {}
    for el in root.iter():
        if el.tag == "UserObject":
            cell = el.find("mxCell")
            cells[el.get("id")] = (cell.get("style", ""), cell.get("parent"), el.get("label", ""), cell)
        elif el.tag == "mxCell" and el.get("id") is not None:
            cells[el.get("id")] = (el.get("style", ""), el.get("parent"), el.get("value", ""), el)
    return cells


class RenderTest(unittest.TestCase):
    samples = {"aws": aws_topology, "network": network_topology, "hybrid": hybrid_topology}

    def test_samples_are_valid(self):
        for name, build in self.samples.items():
            self.assertEqual(build().validate(), [], name)

    # -- draw.io ----------------------------------------------------------------

    def test_drawio_structure(self):
        for name, build in self.samples.items():
            with self.subTest(name):
                topo = build()
                text = render(topo, "drawio")
                root = parse(text)
                self.assertEqual(root.tag, "mxfile")
                self.assertEqual(root.get("compressed"), "false")
                self.assertIsNotNone(root.find("diagram/mxGraphModel/root"))
                cells = drawio_cells(root)
                for node in topo:
                    self.assertIn(node.id, cells, node.id)
                    expected_parent = node.parent if node.parent in topo.nodes else "1"
                    self.assertEqual(cells[node.id][1], expected_parent, node.id)
                    if node.parent:
                        self.assertIn("container=1", cells[node.parent][0])
                for cid, (style, parent, _, el) in cells.items():
                    if el.get("edge") == "1":
                        self.assertEqual(parent, "1")
                        self.assertIn(el.get("source"), cells)
                        self.assertIn(el.get("target"), cells)

    def test_drawio_uses_builtin_shapes(self):
        cells = drawio_cells(parse(render(hybrid_topology(), "drawio")))
        expect = {
            f"aws:account:{ACCT}": "grIcon=mxgraph.aws4.group_account",
            REGION: "grIcon=mxgraph.aws4.group_region",
            "aws:vpc:vpc-00": "grIcon=mxgraph.aws4.group_vpc2",
            "aws:subnet:subnet-000": "fillColor=#F2F6E8",
            "aws:subnet:subnet-001": "fillColor=#E6F6F7",
            "aws:internet-gateway:igw-00": "shape=mxgraph.aws4.internet_gateway",
            "aws:nat-gateway:nat-00": "shape=mxgraph.aws4.nat_gateway",
            "aws:transit-gateway:tgw-01": "resIcon=mxgraph.aws4.transit_gateway",
            "aws:vpn-gateway:vgw-01": "shape=mxgraph.aws4.vpn_gateway",
            "aws:customer-gateway:cgw-01": "shape=mxgraph.aws4.customer_gateway",
            "aws:dx-connection:dxcon-01": "resIcon=mxgraph.aws4.direct_connect",
            "aws:vpc-endpoint:vpce-01": "shape=mxgraph.aws4.endpoints",
            "aws:instance:i-0000": "shape=mxgraph.aws4.instance2",
            "net:device:rtr1": "prIcon=router",
            "net:device:core1": "prIcon=l3_switch",
            "net:device:acc1": "prIcon=l2_switch",
            "net:device:fw1": "prIcon=firewall",
            "net:device:ap1": "mxgraph.cisco19.wireless_access_point2",
            "net:device:phone1": "mxgraph.cisco19.ip_phone2",
            "net:device:srv1": "mxgraph.cisco19.server2",
            "net:device:lab1": "rounded=1",
        }
        for nid, fragment in expect.items():
            self.assertIn(fragment, cells[nid][0], nid)
        self.assertIn("dashed=1", cells["aws:vpc:vpc-0ext"][0])

    def test_drawio_coordinates_relative_to_parent(self):
        topo = hybrid_topology()
        lay = layout(topo)
        cells = drawio_cells(parse(render(topo, "drawio")))
        for nid, box in lay.boxes.items():
            geo = cells[nid][3].find("mxGeometry")
            x, y = int(geo.get("x")), int(geo.get("y"))
            ax, ay = (box.x, box.y) if box.container else box.icon[:2]
            if box.parent:
                ax, ay = ax - lay.boxes[box.parent].x, ay - lay.boxes[box.parent].y
            self.assertEqual((x, y), (ax, ay), nid)

    def test_drawio_link_labels(self):
        text = render(hybrid_topology(), "drawio")
        cells = drawio_cells(parse(text))
        vpn = [c for c in cells.values() if c[3].get("edge") and "vpn-01" in c[2]]
        self.assertEqual(len(vpn), 1)
        self.assertIn("1/2 tunnels up", vpn[0][2])
        self.assertIn("dxvif-01", text)
        self.assertIn("GigabitEthernet0/0/1", text)
        plain = drawio_cells(parse(render(hybrid_topology(), "drawio", link_labels=False)))
        for _, _, value, el in plain.values():
            if el.get("edge") == "1":
                self.assertEqual(value, "")
        self.assertFalse(any(cid.endswith("#src") for cid in plain))

    # -- geometry -----------------------------------------------------------------

    def assert_geometry(self, topo: Topology, direction: str = "TB"):
        lay = layout(topo, direction)
        self.assertEqual(set(lay.boxes), set(topo.nodes))
        siblings = {}
        for box in lay.boxes.values():
            siblings.setdefault(box.parent, []).append(box)
            self.assertGreaterEqual(box.x, 0)
            self.assertGreaterEqual(box.y, 0)
            self.assertLessEqual(box.x + box.w, lay.width)
            self.assertLessEqual(box.y + box.h, lay.height)
            if box.parent:
                p = lay.boxes[box.parent]
                self.assertTrue(p.x <= box.x and box.x + box.w <= p.x + p.w and
                                p.y + p.header <= box.y and box.y + box.h <= p.y + p.h,
                                f"{box.id} escapes {p.id}")
        for group in siblings.values():
            group.sort(key=lambda b: (b.x, b.id))
            for i, a in enumerate(group):
                for b in group[i + 1:]:
                    if b.x >= a.x + a.w:
                        break
                    overlap = (a.x < b.x + b.w and b.x < a.x + a.w and
                               a.y < b.y + b.h and b.y < a.y + a.h)
                    self.assertFalse(overlap, f"{a.id} overlaps {b.id}")
        return lay

    def test_children_inside_parents_and_no_overlap(self):
        for name, build in self.samples.items():
            for direction in ("TB", "LR"):
                with self.subTest(name=name, direction=direction):
                    self.assert_geometry(build(), direction)

    def test_aws_conventions(self):
        lay = self.assert_geometry(aws_topology())
        b = lay.boxes
        az_a, az_b = b["aws:az:vpc-00:us-east-1a"], b["aws:az:vpc-00:us-east-1b"]
        self.assertLess(az_a.x + az_a.w, az_b.x)                 # AZs side by side
        self.assertEqual(az_a.y, az_b.y)
        pub, priv = b["aws:subnet:subnet-000"], b["aws:subnet:subnet-001"]
        self.assertLess(pub.y + pub.h, priv.y)                   # public above private
        self.assertEqual(pub.y, b["aws:subnet:subnet-010"].y)    # rows line up across AZs
        igw = b["aws:internet-gateway:igw-00"]
        self.assertLess(igw.y + igw.h, az_a.y)                   # IGW along the top edge
        tgw = b["aws:transit-gateway:tgw-01"]
        self.assertEqual(tgw.parent, REGION)
        self.assertLess(tgw.y + tgw.h, b["aws:vpc:vpc-00"].y)    # TGW outside, above the VPCs
        self.assertLess(b["aws:customer-gateway:cgw-01"].y, tgw.y)

    def test_network_reads_top_down(self):
        b = layout(network_topology()).boxes
        order = ["ext:external:internet", "net:device:fw1", "net:device:rtr1", "net:device:core1",
                 "net:device:acc1", "net:device:ap1"]
        ys = [b[n].y for n in order if b[n].parent == "net:site:hq"]
        self.assertEqual(ys, sorted(ys))
        self.assertLess(b["net:device:fw1"].y, b["net:device:rtr1"].y)
        self.assertLess(b["net:device:rtr1"].y, b["net:device:core1"].y)
        self.assertLess(b["net:device:core1"].y, b["net:device:acc1"].y)
        self.assertLess(b["net:device:acc1"].y, b["net:device:ap1"].y)
        self.assertEqual(b["net:device:core1"].y, b["net:device:core2"].y)

    def test_ancestor_links_are_not_drawn(self):
        lay = layout(aws_topology())
        self.assertTrue(any(l.startswith("vgw-attachment") for l in lay.skipped))
        for route in lay.routes:
            self.assertGreaterEqual(len(route.points), 2)

    def test_parallel_links_are_separated(self):
        lay = layout(network_topology())
        pair = [r for r in lay.routes if {r.link.source, r.link.target} ==
                {"net:device:core1", "net:device:core2"}]
        self.assertEqual(len(pair), 2)
        self.assertNotEqual(pair[0].points, pair[1].points)

    def test_bad_parents_do_not_break_layout(self):
        t = Topology()
        t.add_node("a", "site", "a", "b")
        t.add_node("b", "site", "b", "a")
        t.add_node("c", "device", "c", "missing", role="router")
        lay = self.assert_geometry(t)
        self.assertEqual(set(lay.boxes), {"a", "b", "c"})
        for fmt in FORMATS:
            self.assertTrue(render(t, fmt))

    def test_empty_topology(self):
        for fmt in FORMATS:
            self.assertTrue(render(Topology(), fmt))
        parse(render(Topology(), "svg"))
        parse(render(Topology(), "drawio"))

    # -- SVG ------------------------------------------------------------------------

    def test_svg(self):
        for name, build in self.samples.items():
            with self.subTest(name):
                topo = build()
                root = parse(render(topo, "svg", title="Example"))
                self.assertTrue(root.tag.endswith("svg"))
                ids = {el.get("data-id") for el in root.iter() if el.get("data-id")}
                for node in topo:
                    self.assertIn(node.id, ids)
                for link in topo.links.values():
                    if not link.kind == "vgw-attachment":
                        self.assertIn(link.id, ids)
                self.assertNotIn("<image", render(topo, "svg"))

    # -- Mermaid and DOT --------------------------------------------------------------

    def test_mermaid(self):
        topo = hybrid_topology()
        text = render(topo, "mermaid", direction="LR")
        self.assertTrue(text.startswith("flowchart LR\n"))
        self.assertIn("subgraph n_aws_vpc_vpc_00[", text)
        subgraphs = len(re.findall(r"^\s*subgraph ", text, re.M))
        containers = [n for n in topo if n.kind in ("account", "region", "vpc", "az", "subnet",
                                                    "site")]
        self.assertEqual(subgraphs, len(containers))
        self.assertEqual(subgraphs, len(re.findall(r"^\s*end$", text, re.M)))
        edges = [l for l in text.splitlines() if re.search(r" (---|-\.-|===|-->|-\.->|==>)", l)]
        drawn = [l for l in topo.links.values() if l.kind != "vgw-attachment"]
        self.assertEqual(len(edges), len(drawn))
        for line in text.splitlines():
            if line.strip().startswith(("n_", "subgraph")):
                label = re.search(r'"(.*)"', line)
                if label:
                    self.assertNotIn('"', label.group(1))
                    self.assertNotIn("<lab>", label.group(1))
        self.assertIn("vpn-01", text)

    def test_dot(self):
        topo = hybrid_topology()
        text = render(topo, "dot")
        self.assertTrue(text.startswith("graph topomap {"))
        self.assertIn("compound=true;", text)
        self.assertIn('subgraph "cluster_aws:vpc:vpc-00" {', text)
        self.assertEqual(text.count("{"), text.count("}"))
        edges = [l for l in text.splitlines() if " -- " in l]
        drawn = [l for l in topo.links.values() if l.kind != "vgw-attachment"]
        self.assertEqual(len(edges), len(drawn))
        self.assertIn('lhead="cluster_aws:vpc:vpc-01"', text)
        for line in text.splitlines():
            unescaped = re.sub(r'\\.', "", line)
            self.assertEqual(unescaped.count('"') % 2, 0, line)

    # -- escaping, determinism, scale, CLI --------------------------------------------

    def test_special_characters_escaped(self):
        topo = network_topology()
        drawio = render(topo, "drawio")
        cells = drawio_cells(parse(drawio))
        self.assertIn("R&amp;D &lt;lab&gt; &quot;core&quot;", cells["net:device:lab1"][2])
        self.assertNotIn("<lab>", drawio)
        svg = render(topo, "svg")
        texts = [el.text for el in parse(svg).iter() if el.tag.endswith("text")]
        self.assertIn(SPECIAL, texts)
        self.assertNotIn("<lab>", svg)
        mmd = render(topo, "mermaid")
        self.assertIn("R#amp;D #lt;lab#gt; #quot;core#quot;", mmd)
        dot = render(topo, "dot")
        self.assertIn('R&amp;D <lab> \\"core\\"', dot)

    def test_deterministic(self):
        for fmt in FORMATS:
            for direction in ("TB", "LR"):
                self.assertEqual(render(hybrid_topology(), fmt, direction=direction),
                                 render(hybrid_topology(), fmt, direction=direction))

    def test_deterministic_across_hash_seeds(self):
        code = ("import sys; sys.path.insert(0, %r); sys.path.insert(0, %r);"
                "from test_render import hybrid_topology; from topomap.render import render;"
                "sys.stdout.write(''.join(render(hybrid_topology(), f) for f in "
                "('drawio', 'svg', 'mermaid', 'dot')))" % (os.path.dirname(HERE), HERE))
        outs = set()
        for seed in ("1", "2"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            outs.add(subprocess.run([sys.executable, "-c", code], env=env, check=True,
                                    capture_output=True, text=True).stdout)
        self.assertEqual(len(outs), 1)

    def test_scale(self):
        topo = big_topology()
        self.assertGreaterEqual(len(topo.nodes), 300)
        start = time.monotonic()
        for fmt in FORMATS:
            render(topo, fmt)
        self.assert_geometry(topo)
        self.assertLess(time.monotonic() - start, 20)

    def test_api_rejects_bad_options(self):
        with self.assertRaises(ValueError):
            render(Topology(), "png")
        with self.assertRaises(ValueError):
            render(Topology(), "svg", direction="BT")
        with self.assertRaises(TypeError):
            render(Topology(), "svg", colour="red")

    def test_infer_format(self):
        self.assertEqual(infer_format("x.drawio"), "drawio")
        self.assertEqual(infer_format("x.XML"), "drawio")
        self.assertEqual(infer_format("x.svg"), "svg")
        self.assertEqual(infer_format("x.mmd"), "mermaid")
        self.assertEqual(infer_format("x.gv"), "dot")
        self.assertEqual(infer_format("-"), "drawio")
        self.assertEqual(infer_format("x.txt"), "drawio")

    def test_cli(self):
        from topomap.cli import main
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in.json")
            hybrid_topology().save(src)
            for fmt in FORMATS:
                out = os.path.join(tmp, f"out.{EXT[fmt]}")
                self.assertEqual(main(["render", src, "-o", out, "--title", "HQ & AWS"]), 0)
                with open(out, encoding="utf-8") as f:
                    text = f.read()
                self.assertEqual(text, render(Topology.load(src), fmt, title="HQ & AWS"))
            out = os.path.join(tmp, "forced.svg")
            self.assertEqual(main(["render", src, "-f", "dot", "-o", out, "--direction", "LR",
                                   "--no-link-labels"]), 0)
            with open(out, encoding="utf-8") as f:
                self.assertIn("rankdir=LR;", f.read())

    def test_fixtures(self):
        """Example outputs kept for people to open and eyeball. Regenerate
        with TOPOMAP_REGEN_FIXTURES=1 after a deliberate change."""
        topo = hybrid_topology()
        os.makedirs(FIXTURES, exist_ok=True)
        expected = {"hybrid.json": topo.dumps()}
        for fmt in FORMATS:
            expected[f"hybrid.{EXT[fmt]}"] = render(topo, fmt, title="HQ and AWS (example)")
        for name, text in expected.items():
            path = os.path.join(FIXTURES, name)
            if REGEN or not os.path.exists(path):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), text, f"{name} is stale; regenerate with "
                                                 "TOPOMAP_REGEN_FIXTURES=1")


if __name__ == "__main__":
    unittest.main()
