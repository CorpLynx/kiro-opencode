import contextlib
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from topomapper.model import Topology, union  # noqa: E402
from topomapper.render import infer_format, render, styles  # noqa: E402
from topomapper.render.layout import layout  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures", "render")
REGEN = os.environ.get("TOPOMAPPER_REGEN_FIXTURES") == "1"
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
               tunnels=[{"outside_ip": "198.51.100.1", "inside_cidr": "10.255.10.0/30",
                         "status": "UP"},
                        {"outside_ip": "198.51.100.2", "inside_cidr": "10.255.11.0/30",
                         "status": "DOWN"}])
    dxcon = t.add_node("aws:dx-connection:dxcon-01", "dx-connection", "dxcon-01", REGION,
                       connection_id="dxcon-01", location="example-dx-site-1", bandwidth="1Gbps").id
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


def org_topology() -> Topology:
    """The organization `aws_topology`'s account belongs to, the way
    `topomapper org collect` sees it: OUs, a second account and policies."""
    t = Topology({"collector": "test"})
    org = t.add_node("aws:organization:o-example0001", "organization", "Organization (o-example0001)",
                     org_id="o-example0001").id
    root = t.add_node("aws:org-root:r-0000", "org-root", "Root", org, root_id="r-0000",
                      policies={"scp": ["FullAWSAccess"]}).id
    ou = t.add_node("aws:ou:ou-0000-prod0001", "ou", "Prod", root, ou_id="ou-0000-prod0001",
                    policies={"scp": ["FullAWSAccess", "DenyRegions"]}).id
    t.add_node(f"aws:account:{ACCT}", "account", "example-prod", ou, account_id=ACCT)
    t.add_node("aws:account:111122223333", "account", "example-management", root,
               account_id="111122223333", management=True)
    for pid, name, target in (("p-FullAWSAccess", "FullAWSAccess", root),
                              ("p-0000deny0001", "DenyRegions", ou)):
        t.add_node(f"aws:org-policy:{pid}", "org-policy", name, org, policy_id=pid, type="scp")
        t.add_link("policy-attachment", f"aws:org-policy:{pid}", target, directed=True, type="scp")
    return t


def estate_topology() -> Topology:
    """An account's networks inside its organization: what `topomapper topology merge`
    makes of `aws-org` and `aws` snapshots."""
    t = union([org_topology(), aws_topology()])
    t.meta = {"collector": "test"}
    return t


def big_topology(n_vpcs: int = 12, per_subnet: int = 5) -> Topology:
    t = Topology()
    acct = t.add_node(f"aws:account:{ACCT}", "account", "big").id
    region = t.add_node(REGION, "region", "us-east-1", acct).id
    tgw = t.add_node("aws:transit-gateway:tgw-big", "transit-gateway", "tgw-big", region).id
    for v in range(n_vpcs):
        vpc = t.add_node(f"aws:vpc:vpc-{v:02d}", "vpc", f"vpc-{v:02d}", region).id
        t.add_link("tgw-attachment", vpc, tgw, key=f"tgw-attach-{v:02d}")
        for az in ("a", "b"):
            azid = t.add_node(f"aws:az:vpc-{v:02d}:us-east-1{az}", "az", f"us-east-1{az}", vpc).id
            for public in (True, False):
                sub = t.add_node(f"aws:subnet:subnet-{v:02d}{az}{int(public)}", "subnet",
                                 f"subnet-{v:02d}{az}{int(public)}", azid, public=public).id
                for i in range(per_subnet):
                    t.add_node(f"aws:instance:i-{v:02d}{az}{int(public)}{i}", "instance",
                               f"i-{v:02d}{az}{int(public)}{i}", sub)
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
    samples = {"aws": aws_topology, "org": org_topology, "estate": estate_topology}

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
        cells = drawio_cells(parse(render(estate_topology(), "drawio")))
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
            "aws:account:111122223333": "mxgraph.aws4.organizations_management_account",
            "aws:org-policy:p-0000deny0001": "rounded=1",
        }
        for nid, fragment in expect.items():
            self.assertIn(fragment, cells[nid][0], nid)
        self.assertIn("dashed=1", cells["aws:vpc:vpc-0ext"][0])

    def test_drawio_coordinates_relative_to_parent(self):
        topo = estate_topology()
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
        text = render(estate_topology(), "drawio")
        cells = drawio_cells(parse(text))
        vpn = [c for c in cells.values() if c[3].get("edge") and "vpn-01" in c[2]]
        self.assertEqual(len(vpn), 1)
        self.assertIn("1/2 tunnels up", vpn[0][2])
        self.assertIn("dxvif-01", text)
        self.assertIn("tgw-attach-00", text)
        plain = drawio_cells(parse(render(estate_topology(), "drawio", link_labels=False)))
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

    def test_ancestor_links_are_not_drawn(self):
        lay = layout(aws_topology())
        self.assertTrue(any(l.startswith("vgw-attachment") for l in lay.skipped))
        for route in lay.routes:
            self.assertGreaterEqual(len(route.points), 2)

    def test_parallel_links_are_separated(self):
        topo = aws_topology()  # a second, redundant VPN between the same gateways
        topo.add_link("vpn", "aws:customer-gateway:cgw-01", "aws:transit-gateway:tgw-01",
                      key="vpn-02", vpn_id="vpn-02", state="available")
        pair = [r for r in layout(topo).routes if r.link.kind == "vpn"]
        self.assertEqual(len(pair), 2)
        self.assertNotEqual(pair[0].points, pair[1].points)

    def test_bad_parents_do_not_break_layout(self):
        t = Topology()
        t.add_node("a", "vpc", "a", "b")
        t.add_node("b", "vpc", "b", "a")
        t.add_node("c", "instance", "c", "missing")
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
        topo = estate_topology()
        text = render(topo, "mermaid", direction="LR")
        self.assertTrue(text.startswith("flowchart LR\n"))
        self.assertIn("subgraph n_aws_vpc_vpc_00[", text)
        subgraphs = len(re.findall(r"^\s*subgraph ", text, re.M))
        classes = styles.classify(topo, styles.effective_parents(topo))
        containers = [n for n, c in classes.items() if styles.is_container_class(c)]
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
        topo = estate_topology()
        text = render(topo, "dot")
        self.assertTrue(text.startswith("graph topomapper {"))
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
        topo = org_topology()
        topo.add_node("aws:org-policy:p-0000lab00001", "org-policy", SPECIAL,
                      "aws:organization:o-example0001")
        drawio = render(topo, "drawio")
        cells = drawio_cells(parse(drawio))
        self.assertIn("R&amp;D &lt;lab&gt; &quot;core&quot;", cells["aws:org-policy:p-0000lab00001"][2])
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
                self.assertEqual(render(estate_topology(), fmt, direction=direction),
                                 render(estate_topology(), fmt, direction=direction))

    def test_deterministic_across_hash_seeds(self):
        code = ("import sys; sys.path.insert(0, %r); sys.path.insert(0, %r);"
                "from test_render import estate_topology; from topomapper.render import render;"
                "sys.stdout.write(''.join(render(estate_topology(), f) for f in "
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
        from topomapper.cli import main
        with tempfile.TemporaryDirectory() as tmp:
            src = os.path.join(tmp, "in.json")
            estate_topology().save(src)
            outs = {fmt: os.path.join(tmp, f"out.{EXT[fmt]}") for fmt in FORMATS}
            self.assertEqual(main(["diagram", "draw", src, *outs.values(), "--title", "Prod & AWS"]), 0)
            for fmt, out in outs.items():
                with open(out, encoding="utf-8") as f:
                    text = f.read()
                self.assertEqual(text, render(Topology.load(src), fmt, title="Prod & AWS"))
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                self.assertEqual(main(["diagram", "draw", src, "--format", "dot", "--direction", "LR",
                                       "--no-link-labels"]), 0)
            self.assertIn("rankdir=LR;", stdout.getvalue())

    def test_fixtures(self):
        """Example outputs kept for people to open and eyeball. Regenerate
        with TOPOMAPPER_REGEN_FIXTURES=1 after a deliberate change."""
        topo = estate_topology()
        os.makedirs(FIXTURES, exist_ok=True)
        expected = {"estate.json": topo.dumps()}
        for fmt in FORMATS:
            expected[f"estate.{EXT[fmt]}"] = render(topo, fmt, title="An account in its organization (example)")
        for name, text in expected.items():
            path = os.path.join(FIXTURES, name)
            if REGEN or not os.path.exists(path):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(text)
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), text, f"{name} is stale; regenerate with "
                                                 "TOPOMAPPER_REGEN_FIXTURES=1")


def ai_topology() -> Topology:
    """An organization with Bedrock, IAM and EKS in two accounts."""
    t = Topology({"collector": "test"})
    org = t.add_node("aws:organization:o-example0001", "organization", "Organization").id
    ids = {"111122223333": "prod", "444455556666": "dev"}
    for account, name in ids.items():
        acct = t.add_node(f"aws:account:{account}", "account", f"{name} ({account})", org,
                          account_id=account).id
        region = t.add_node(f"aws:region:{account}:us-east-1", "region", "us-east-1", acct).id
        group = t.add_node(f"aws:bedrock:{account}:us-east-1", "bedrock", "Amazon Bedrock",
                           region, models_shown=2).id
        for model, provider in (("Claude A", "Anthropic"), ("Titan B", "Amazon")):
            t.add_node(f"aws:bedrock-model:{account}:us-east-1:{model}", "bedrock-model", model,
                       group, provider=provider, source="foundation")
        vpc = t.add_node(f"aws:vpc:vpc-{name}", "vpc", f"{name}-vpc", region, cidrs=["10.0.0.0/16"]).id
        t.add_node(f"aws:eks-cluster:{account}:us-east-1:platform", "eks-cluster", "platform", vpc,
                   version="1.30", endpoint_public_access=True)
        box = t.add_node(f"aws:iam:{account}", "iam", "IAM", acct, summary="1 role, 1 user").id
        t.add_node(f"aws:iam-role:{account}:deploy", "iam-role", "deploy", box,
                   trusted_accounts=["444455556666"], admin_access=True)
        t.add_node(f"aws:iam-user:{account}:alice", "iam-user", "alice", box, mfa_enabled=False,
                   active_access_keys=1)
    t.add_link("iam-trust", "aws:account:444455556666", "aws:iam-role:111122223333:deploy",
               directed=True, principal="root")
    return t


class AiRenderTest(unittest.TestCase):
    def test_every_format_renders_the_new_kinds(self):
        t = ai_topology()
        self.assertEqual(t.validate(), [])
        for fmt in FORMATS:
            with self.subTest(fmt=fmt):
                text = render(t, fmt)
                for word in ("Amazon Bedrock", "Claude A", "deploy", "alice", "platform"):
                    self.assertIn(word, text)
        self.assertIn("trusts 1 account(s)", render(t, "svg"))
        self.assertIn("admin access", render(t, "svg"))
        self.assertIn("no MFA", render(t, "svg"))
        self.assertIn("Kubernetes 1.30", render(t, "svg"))
        self.assertIn("2 in use or granted", render(t, "svg"))

    def test_drawio_uses_the_aws_icons_and_valid_xml(self):
        text = render(ai_topology(), "drawio")
        ET.fromstring(text)
        self.assertIn("resIcon=mxgraph.aws4.eks", text)
        self.assertIn("strokeColor=#01A88D", text)       # Bedrock group and models
        self.assertIn("strokeColor=#DD344C", text)       # IAM role and the trust link

    def test_drawio_iam_box_and_eks_use_awss_own_icons(self):
        text = render(ai_topology(), "drawio")
        root = ET.fromstring(text)
        cells = {c.get("id"): c for c in root.iter("mxCell")}
        for u in root.iter("UserObject"):
            cells[u.get("id")] = u.find("mxCell")
        logo = cells["aws:iam:111122223333#logo"]
        self.assertIn("shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4."
                      "identity_and_access_management;", logo.get("style"))
        self.assertIn("fillColor=#DD344C", logo.get("style"))
        self.assertEqual(logo.get("parent"), "aws:iam:111122223333")
        geometry = logo.find("mxGeometry")
        self.assertEqual([int(geometry.get(k)) for k in ("x", "y", "width", "height")], [8, 6, 30, 30])
        self.assertIn("spacingLeft=46", cells["aws:iam:111122223333"].get("style"))
        eks = cells["aws:eks-cluster:111122223333:us-east-1:platform"].get("style")
        self.assertIn("shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.eks;", eks)
        self.assertIn("fillColor=#ED7100", eks)
        ids = [e.get("id") for e in root.iter() if e.tag in ("mxCell", "UserObject") and e.get("id")]
        self.assertEqual(len(ids), len(set(ids)))

    def test_drawio_roles_users_and_bedrock_use_awss_own_shapes(self):
        root = ET.fromstring(render(ai_topology(), "drawio"))
        styles = {}
        for u in root.iter("UserObject"):
            styles[u.get("id")] = u.find("mxCell").get("style")
        for c in root.iter("mxCell"):
            if c.get("id"):
                styles[c.get("id")] = c.get("style")
        role = styles["aws:iam-role:111122223333:deploy"]
        user = styles["aws:iam-user:111122223333:alice"]
        logo = styles["aws:iam:111122223333#logo"]
        # A role wears the IAM box's own icon; it only adds the white label background
        # that every icon with its name underneath gets.
        self.assertEqual(role.replace("labelBackgroundColor=#FFFFFF;", ""), logo)
        self.assertIn("resIcon=mxgraph.aws4.identity_and_access_management;", role)
        self.assertNotIn("mxgraph.aws4.role", role)
        self.assertIn("fillColor=#DD344C", role)
        self.assertIn("shape=mxgraph.aws4.user;", user)
        self.assertIn("fillColor=#232F3D", user)
        for node in ("aws:iam-role:111122223333:deploy", "aws:iam-user:111122223333:alice"):
            self.assertIn("verticalLabelPosition=bottom", styles[node])   # name under the icon
        self.assertIn("resIcon=mxgraph.aws4.bedrock;", styles["aws:bedrock:111122223333:us-east-1#logo"])
        self.assertIn("fillColor=#01A88D", styles["aws:bedrock:111122223333:us-east-1#logo"])
        self.assertIn("spacingLeft=46", styles["aws:bedrock:111122223333:us-east-1"])

    def test_svg_has_matching_tiles(self):
        svg = render(ai_topology(), "svg")
        for word in (">USER</text>", ">AI</text>", ">IAM</text>"):
            self.assertIn(word, svg)
        lay = layout(ai_topology())
        for box in lay.boxes.values():
            if box.vclass in ("iam-role", "iam-user"):
                self.assertFalse(box.container)
                self.assertGreaterEqual(box.h, box.icon[3] + 15)       # label sits below the icon

    def test_the_logo_fits_in_the_title_bar_without_covering_the_title(self):
        lay = layout(ai_topology())
        for nid, box in lay.boxes.items():
            if box.vclass in ("iam", "bedrock"):
                self.assertGreaterEqual(box.header, 36)
                self.assertGreaterEqual(box.w, 36 + 8 + 30)

    def test_svg_iam_box_has_a_logo(self):
        svg = render(ai_topology(), "svg")
        self.assertIn(">IAM</text>", svg)
        ET.fromstring(svg)

    def test_geometry_holds_with_the_new_kinds(self):
        t = ai_topology()
        for direction in ("TB", "LR"):
            lay = layout(t, direction=direction)
            for box in lay.boxes.values():
                parent = lay.boxes.get(box.parent) if box.parent else None
                if parent is not None:
                    self.assertGreaterEqual(box.x, parent.x)
                    self.assertGreaterEqual(box.y, parent.y)
                    self.assertLessEqual(box.x + box.w, parent.x + parent.w)
                    self.assertLessEqual(box.y + box.h, parent.y + parent.h)
        self.assertEqual(render(t, "svg"), render(ai_topology(), "svg"))
        ET.fromstring(render(t, "svg"))

    def test_iam_box_is_pinned_to_the_accounts_top_right_corner(self):
        t = ai_topology()
        # An account whose other contents are tall and wide, and one with only IAM.
        for i in range(6):
            t.add_node(f"aws:vpc:vpc-extra{i}", "vpc", f"extra{i}",
                       "aws:region:111122223333:us-east-1", cidrs=["10.1.0.0/16"])
        t.add_node("aws:account:777788889999", "account", "iam-only", "aws:organization:o-example0001")
        t.add_node("aws:iam:777788889999", "iam", "IAM", "aws:account:777788889999")
        t.add_node("aws:iam-role:777788889999:r", "iam-role", "r", "aws:iam:777788889999")
        for direction in ("TB", "LR"):
            boxes = layout(t, direction=direction).boxes
            for account in ("111122223333", "444455556666", "777788889999"):
                acct, iam = boxes[f"aws:account:{account}"], boxes[f"aws:iam:{account}"]
                self.assertEqual(iam.parent, acct.id)
                self.assertEqual(iam.y, min(b.y for b in boxes.values() if b.parent == acct.id))
                self.assertEqual(iam.x, max(b.x for b in boxes.values() if b.parent == acct.id))
                others = [b for b in boxes.values() if b.parent == acct.id and b.id != iam.id]
                for b in others:
                    self.assertLessEqual(b.x + b.w, iam.x)

    def test_iam_box_text_and_roles_inside(self):
        svg = render(ai_topology(), "svg")
        self.assertIn("1 role, 1 user", svg)
        self.assertIn('class="container iam"', svg)

    def test_mermaid_and_dot_nest_bedrock_in_the_region(self):
        self.assertRegex(render(ai_topology(), "mermaid"),
                         r"subgraph n_aws_bedrock_111122223333_us_east_1\[")
        self.assertIn("cluster_aws:bedrock:111122223333:us-east-1", render(ai_topology(), "dot"))


class OrgLabelTest(unittest.TestCase):
    def lines(self, kind, **attrs):
        from topomapper.model import Node
        from topomapper.render.labels import node_lines
        return node_lines(Node(id=f"x:{kind}:1", kind=kind, label="Name", attrs=attrs))

    def test_status_lines(self):
        self.assertIn("not collected", self.lines("account", account_id="111111111111",
                                                  collected=False))
        self.assertNotIn("not collected", self.lines("account", account_id="111111111111",
                                                     collected=True))
        self.assertIn("no resources", self.lines("region", region="us-east-1", empty=True))
        self.assertIn("empty", self.lines("ou", ou_id="ou-aaaa-bbbbbbbb", empty=True))


if __name__ == "__main__":
    unittest.main()
