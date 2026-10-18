import contextlib
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from topomap import cli  # noqa: E402
from topomap.filter import filter_topology  # noqa: E402
from topomap.merge import merge  # noqa: E402
from topomap.model import Topology  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "merge")

ACCOUNT = "aws:account:123456789012"
REGION = "aws:region:123456789012:us-east-1"
VPC = "aws:vpc:vpc-0a1b2c3d4e5f60001"
AZ = "aws:az:vpc-0a1b2c3d4e5f60001:us-east-1a"
SUBNET = "aws:subnet:subnet-0a1b2c3d4e5f60001"
INSTANCE = "aws:instance:i-0a1b2c3d4e5f60001"
TGW = "aws:transit-gateway:tgw-0a1b2c3d4e5f60001"
CGW_HQ = "aws:customer-gateway:cgw-0a1b2c3d4e5f60001"
DXCON = "aws:dx-connection:dxcon-ffa1b2c3"
SITE = "net:site:hq"
EDGE1, DXR1, CORE1, BRANCH1 = ("net:device:edge1", "net:device:dxr1", "net:device:core1",
                               "net:device:branch1")


def merged():
    """The merge fixtures, stitched: an AWS estate and an HQ site, joined at
    edge1 (VPN) and dxr1 (Direct Connect)."""
    return merge([Topology.load(os.path.join(FIXTURES, "aws.json")),
                  Topology.load(os.path.join(FIXTURES, "network.json"))])


def two_sites():
    """Two sites with three cables between them and one inside site a."""
    t = Topology()
    for site in ("a", "b"):
        t.add_node(f"net:site:{site}", "site", site.upper())
    for name, site in (("a1", "a"), ("a2", "a"), ("b1", "b")):
        t.add_node(f"net:device:{name}", "device", name, f"net:site:{site}")
    t.add_node("net:device:wan", "device", "wan")
    t.add_link("physical", "net:device:a1", "net:device:b1", source_port="Gi0/1", target_port="Gi0/1",
               protocols=["cdp"])
    t.add_link("physical", "net:device:a2", "net:device:b1", source_port="Gi0/1", target_port="Gi0/2",
               protocols=["cdp"])
    t.add_link("physical", "net:device:a1", "net:device:a2", source_port="Gi0/2", target_port="Gi0/2")
    t.add_link("physical", "net:device:wan", "net:device:a1", source_port="Gi0/0",
               target_port="Gi0/3")
    t.add_link("physical", "net:device:wan", "net:device:a2", source_port="Gi0/0",
               target_port="Gi0/3")
    return t


class FilterTest(unittest.TestCase):
    def assertValid(self, topo):
        self.assertEqual(topo.validate(), [])

    def test_no_options_copies(self):
        topo = merged()
        out = filter_topology(topo)
        self.assertEqual(out.dumps(), topo.dumps())
        out.nodes[VPC].attrs["cidrs"].append("10.99.0.0/16")
        self.assertEqual(topo.nodes[VPC].attrs["cidrs"], ["10.10.0.0/16"])

    def test_kind_keeps_ancestors_and_links_between_kept(self):
        out = filter_topology(merged(), kinds=["vpc", "transit-gateway"])
        self.assertValid(out)
        self.assertEqual(sorted(out.nodes), sorted([ACCOUNT, REGION, VPC, TGW]))
        self.assertEqual([l.kind for l in out.links.values()], ["tgw-attachment"])
        self.assertEqual(out.meta["filters"], [{"kind": ["transit-gateway", "vpc"]}])

    def test_exclude_container_reparents(self):
        out = filter_topology(merged(), exclude_kinds=["az", "site"])
        self.assertValid(out)
        self.assertNotIn(AZ, out.nodes)
        self.assertEqual(out.nodes[SUBNET].parent, VPC)
        self.assertIsNone(out.nodes[EDGE1].parent)
        self.assertEqual(len(out.links), len(merged().links))

    def test_exclude_leaf_removes_its_links(self):
        out = filter_topology(merged(), exclude_kinds=["customer-gateway"])
        self.assertValid(out)
        self.assertFalse([l for l in out.links.values() if l.kind == "vpn"])
        self.assertEqual([l.attrs["rule"] for l in out.links.values() if l.kind == "stitch"],
                         ["dx-vif"])

    def test_collapse_folds_contents(self):
        out = filter_topology(merged(), collapse=["vpc"])
        self.assertValid(out)
        for inner in (AZ, SUBNET, INSTANCE):
            self.assertNotIn(inner, out.nodes)
        self.assertEqual(out.nodes[VPC].attrs["collapsed"], {"az": 1, "instance": 1, "subnet": 1})

    def test_collapse_repoints_dedupes_and_drops_self_loops(self):
        out = filter_topology(two_sites(), collapse=["site"])
        self.assertValid(out)
        self.assertEqual(sorted(out.nodes), ["net:device:wan", "net:site:a", "net:site:b"])
        between = out.links["physical|net:site:a|net:site:b"]
        self.assertEqual(between.attrs, {"collapsed_links": 2, "protocols": ["cdp"]})
        wan = out.links["physical|net:device:wan|net:site:a"]
        self.assertEqual(wan.attrs, {"collapsed_links": 2, "source_port": "Gi0/0"})
        self.assertEqual(len(out.links), 2)

    def test_collapse_outermost_wins(self):
        out = filter_topology(merged(), collapse=["region", "vpc"])
        self.assertValid(out)
        self.assertNotIn(VPC, out.nodes)
        self.assertEqual(out.nodes[REGION].attrs["collapsed"]["vpc"], 1)
        stitch_ends = sorted({(l.source, l.target) for l in out.links.values() if l.kind == "stitch"})
        self.assertEqual(stitch_ends, [(REGION, DXR1), (REGION, EDGE1)])

    def test_around_with_depth_and_ancestors(self):
        out = filter_topology(merged(), around="edge1", depth=1)
        self.assertValid(out)
        self.assertEqual(sorted(out.nodes), sorted([EDGE1, CORE1, CGW_HQ, SITE, REGION, ACCOUNT]))
        self.assertEqual(out.meta["filters"], [{"around": EDGE1, "depth": 1}])
        out = filter_topology(merged(), around=EDGE1, depth=2)
        self.assertIn(TGW, out.nodes)
        self.assertIn(DXR1, out.nodes)
        self.assertEqual(sorted(filter_topology(merged(), around=EDGE1, depth=0).nodes),
                         sorted([EDGE1, SITE]))

    def test_around_container_includes_contents(self):
        out = filter_topology(merged(), around=VPC, depth=1)
        for expected in (AZ, SUBNET, INSTANCE, TGW):
            self.assertIn(expected, out.nodes)
        self.assertNotIn(EDGE1, out.nodes)

    def test_around_unknown_node(self):
        with self.assertRaisesRegex(ValueError, "no node"):
            filter_topology(merged(), around="nowhere")

    def test_match(self):
        out = filter_topology(merged(), match=r"^(edge|dxr)\d")
        self.assertValid(out)
        self.assertEqual(sorted(out.nodes), sorted([EDGE1, DXR1, SITE]))
        out = filter_topology(merged(), match="(?i)PROD")
        self.assertIn(VPC, out.nodes)
        with self.assertRaisesRegex(ValueError, "--match"):
            filter_topology(merged(), match="(")

    def test_aws_and_network_only(self):
        aws = filter_topology(merged(), aws_only=True)
        net = filter_topology(merged(), network_only=True)
        for out in (aws, net):
            self.assertValid(out)
            self.assertFalse([l for l in out.links.values() if l.kind == "stitch"])
        self.assertTrue(all(i.startswith("aws:") for i in aws.nodes))
        self.assertEqual(sorted(net.nodes), sorted([SITE, EDGE1, DXR1, CORE1, BRANCH1]))
        with self.assertRaises(ValueError):
            filter_topology(merged(), aws_only=True, network_only=True)

    def test_namespace_keeps_linked_externals(self):
        topo = merged()
        topo.add_node("ext:external:internet", "external", "Internet")
        topo.add_link("physical", EDGE1, "ext:external:internet", source_port="GigabitEthernet0/0/0")
        self.assertIn("ext:external:internet", filter_topology(topo, network_only=True).nodes)
        self.assertNotIn("ext:external:internet", filter_topology(topo, aws_only=True).nodes)

    def test_combined(self):
        out = filter_topology(merged(), network_only=True, kinds=["device"], exclude_kinds=["site"],
                              around=EDGE1, depth=2)
        self.assertValid(out)
        self.assertEqual(sorted(out.nodes), sorted([EDGE1, CORE1, DXR1]))
        self.assertTrue(all(n.parent is None for n in out.nodes.values()))
        self.assertEqual(out.meta["filters"], [{"only": "network", "kind": ["device"],
                                                "around": EDGE1, "depth": 2,
                                                "exclude_kind": ["site"]}])
        again = filter_topology(out, collapse=["device"])
        self.assertEqual(len(again.meta["filters"]), 2)

    def test_unknown_kind_and_empty_result(self):
        with self.assertRaisesRegex(ValueError, "unknown kind vpcs"):
            filter_topology(merged(), kinds=["vpcs"])
        out = filter_topology(merged(), match="nothing-matches-this")
        self.assertEqual(out.nodes, {})
        self.assertIn("filter kept no nodes", out.meta["warnings"])


class FilterCliTest(unittest.TestCase):
    def run_cli(self, argv, stdin=""):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch("sys.stdin", io.StringIO(stdin)):
            status = cli.main(argv)
        return status, out.getvalue(), err.getvalue()

    def test_cli_from_stdin(self):
        status, out, _ = self.run_cli(["filter", "--collapse", "vpc", "--exclude-kind", "az",
                                       "--aws-only"], stdin=merged().dumps())
        self.assertEqual(status, 0)
        topo = Topology.loads(out)
        self.assertEqual(topo.validate(), [])
        self.assertNotIn(SUBNET, topo.nodes)
        self.assertEqual(topo.meta["filters"][0]["collapse"], ["vpc"])

    def test_cli_around_and_errors(self):
        status, out, _ = self.run_cli(["filter", "-", "--around", "dxr1", "--depth", "1"],
                                      stdin=merged().dumps())
        self.assertEqual(status, 0)
        self.assertIn(DXCON, Topology.loads(out).nodes)
        status, _, err = self.run_cli(["filter", "--depth", "2"], stdin=merged().dumps())
        self.assertEqual(status, 2)
        self.assertIn("--depth needs --around", err)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["filter", "--aws-only", "--network-only"])


if __name__ == "__main__":
    unittest.main()
