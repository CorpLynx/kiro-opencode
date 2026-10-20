import contextlib
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from topomapper import cli  # noqa: E402
from topomapper.filter import filter_topology  # noqa: E402
from topomapper.merge import merge  # noqa: E402
from topomapper.model import Topology  # noqa: E402

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
CGW_BRANCH = "aws:customer-gateway:cgw-0a1b2c3d4e5f60002"
DXGW = "aws:dx-gateway:5f3e1c2a-0000-4000-8000-000000000001"
OU, ROOT, ORG = "aws:ou:ou-0000-prod0001", "aws:org-root:r-0000", "aws:organization:o-exampleorg1"
ORG_CHART = [ACCOUNT, OU, ROOT, ORG]


def merged():
    """The merge fixtures: an AWS account's networks, inside its organization."""
    return merge([Topology.load(os.path.join(FIXTURES, "org.json")),
                  Topology.load(os.path.join(FIXTURES, "aws.json"))])


def two_vpcs():
    """Two VPCs with three peering links between their instances, one inside
    VPC a, and an outside peer linked to both of a's instances."""
    t = Topology()
    for vpc in ("a", "b"):
        t.add_node(f"aws:vpc:vpc-{vpc}", "vpc", vpc.upper())
    for name, vpc in (("a1", "a"), ("a2", "a"), ("b1", "b")):
        t.add_node(f"aws:instance:i-{name}", "instance", name, f"aws:vpc:vpc-{vpc}")
    t.add_node("ext:external:peer", "external", "peer")
    t.add_link("vpc-peering", "aws:instance:i-a1", "aws:instance:i-b1", source_port="e1",
               target_port="e1", state="active")
    t.add_link("vpc-peering", "aws:instance:i-a2", "aws:instance:i-b1", source_port="e1",
               target_port="e2", state="active")
    t.add_link("vpc-peering", "aws:instance:i-a1", "aws:instance:i-a2", source_port="e2",
               target_port="e2")
    t.add_link("vpc-peering", "ext:external:peer", "aws:instance:i-a1", source_port="p0",
               target_port="e3")
    t.add_link("vpc-peering", "ext:external:peer", "aws:instance:i-a2", source_port="p0",
               target_port="e3")
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
        self.assertEqual(sorted(out.nodes), sorted([REGION, VPC, TGW] + ORG_CHART))
        self.assertEqual([l.kind for l in out.links.values()], ["tgw-attachment"])
        self.assertEqual(out.meta["filters"], [{"kind": ["transit-gateway", "vpc"]}])

    def test_exclude_container_reparents(self):
        out = filter_topology(merged(), exclude_kinds=["az"])
        self.assertValid(out)
        self.assertNotIn(AZ, out.nodes)
        self.assertEqual(out.nodes[SUBNET].parent, VPC)
        self.assertEqual(len(out.links), len(merged().links))

    def test_exclude_leaf_removes_its_links(self):
        out = filter_topology(merged(), exclude_kinds=["customer-gateway"])
        self.assertValid(out)
        self.assertFalse([l for l in out.links.values() if l.kind == "vpn"])
        self.assertTrue([l for l in out.links.values() if l.kind == "dx-vif"])

    def test_collapse_folds_contents(self):
        out = filter_topology(merged(), collapse=["vpc"])
        self.assertValid(out)
        for inner in (AZ, SUBNET, INSTANCE):
            self.assertNotIn(inner, out.nodes)
        self.assertEqual(out.nodes[VPC].attrs["collapsed"], {"az": 1, "instance": 1, "subnet": 1})

    def test_collapse_repoints_dedupes_and_drops_self_loops(self):
        out = filter_topology(two_vpcs(), collapse=["vpc"])
        self.assertValid(out)
        self.assertEqual(sorted(out.nodes), ["aws:vpc:vpc-a", "aws:vpc:vpc-b", "ext:external:peer"])
        between = out.links["vpc-peering|aws:vpc:vpc-a|aws:vpc:vpc-b"]
        self.assertEqual(between.attrs, {"collapsed_links": 2, "state": "active"})
        peer = out.links["vpc-peering|aws:vpc:vpc-a|ext:external:peer"]
        self.assertEqual(peer.attrs, {"collapsed_links": 2, "target_port": "p0"})
        self.assertEqual(len(out.links), 2)  # the link inside VPC a is gone

    def test_collapse_outermost_wins(self):
        out = filter_topology(merged(), collapse=["region", "vpc"])
        self.assertValid(out)
        self.assertNotIn(VPC, out.nodes)
        self.assertEqual(out.nodes[REGION].attrs["collapsed"]["vpc"], 1)
        for link in out.links.values():
            self.assertNotEqual(link.source, link.target)

    def test_around_with_depth_and_ancestors(self):
        out = filter_topology(merged(), around="core-tgw (tgw-0a1b2c3d4e5f60001)", depth=1)
        self.assertValid(out)
        self.assertEqual(sorted(out.nodes),
                         sorted([TGW, VPC, CGW_HQ, CGW_BRANCH, DXGW, REGION] + ORG_CHART))
        self.assertEqual(out.meta["filters"], [{"around": TGW, "depth": 1}])
        out = filter_topology(merged(), around=TGW, depth=2)
        self.assertIn(DXCON, out.nodes)
        self.assertEqual(sorted(filter_topology(merged(), around=TGW, depth=0).nodes),
                         sorted([TGW, REGION] + ORG_CHART))
        out = filter_topology(merged(), around=INSTANCE, depth=0)
        self.assertEqual(sorted(out.nodes), sorted([INSTANCE, SUBNET, AZ, VPC, REGION] + ORG_CHART))

    def test_around_container_includes_contents(self):
        out = filter_topology(merged(), around=VPC, depth=1)
        for expected in (AZ, SUBNET, INSTANCE, TGW):
            self.assertIn(expected, out.nodes)
        self.assertNotIn(CGW_HQ, out.nodes)

    def test_around_unknown_node(self):
        with self.assertRaisesRegex(ValueError, "no node"):
            filter_topology(merged(), around="nowhere")

    def test_match(self):
        out = filter_topology(merged(), match=r"^hq-")
        self.assertValid(out)
        self.assertEqual(sorted(out.nodes), sorted([CGW_HQ, REGION] + ORG_CHART))
        out = filter_topology(merged(), match="(?i)PROD")
        self.assertIn(VPC, out.nodes)
        with self.assertRaisesRegex(ValueError, "--match"):
            filter_topology(merged(), match="(")

    def test_aws_only_keeps_linked_externals(self):
        topo = merged()
        topo.add_node("ext:external:internet", "external", "Internet")
        topo.add_link("vpc-peering", VPC, "ext:external:internet")
        topo.add_node("ext:external:elsewhere", "external", "Elsewhere")
        out = filter_topology(topo, aws_only=True)
        self.assertValid(out)
        self.assertIn("ext:external:internet", out.nodes)
        self.assertNotIn("ext:external:elsewhere", out.nodes)
        self.assertEqual(sorted(i for i in out.nodes if not i.startswith("ext:")),
                         sorted(merged().nodes))

    def test_combined(self):
        out = filter_topology(merged(), aws_only=True, kinds=["vpc", "customer-gateway"],
                              exclude_kinds=["region"], around=TGW, depth=1)
        self.assertValid(out)
        self.assertEqual(sorted(out.nodes), sorted([VPC, CGW_HQ, CGW_BRANCH] + ORG_CHART))
        self.assertEqual(out.nodes[VPC].parent, ACCOUNT)
        self.assertEqual(out.meta["filters"], [{"only": "aws", "kind": ["customer-gateway", "vpc"],
                                                "around": TGW, "depth": 1,
                                                "exclude_kind": ["region"]}])
        again = filter_topology(out, collapse=["account"])
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
        status, out, _ = self.run_cli(["topology", "filter", "-", "--collapse", "vpc", "--exclude-kind", "az",
                                       "--aws-only"], stdin=merged().dumps())
        self.assertEqual(status, 0)
        topo = Topology.loads(out)
        self.assertEqual(topo.validate(), [])
        self.assertNotIn(SUBNET, topo.nodes)
        self.assertEqual(topo.meta["filters"][0]["collapse"], ["vpc"])

    def test_cli_around_and_errors(self):
        status, out, _ = self.run_cli(["topology", "filter", "-", "--around", TGW, "--depth", "1"],
                                      stdin=merged().dumps())
        self.assertEqual(status, 0)
        self.assertIn(CGW_HQ, Topology.loads(out).nodes)
        status, _, err = self.run_cli(["topology", "filter", "-", "--depth", "2"], stdin=merged().dumps())
        self.assertEqual(status, 2)
        self.assertIn("--depth needs --around", err)
        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["topology", "filter", "-", "--network-only"])  # gone with the LAN scanner


class HideEmptyTest(unittest.TestCase):
    def topo(self):
        t = Topology()
        t.add_node("aws:account:111111111111", "account")
        t.add_node("aws:region:111111111111:us-east-1", "region", parent="aws:account:111111111111")
        t.add_node("aws:region:111111111111:eu-west-1", "region", parent="aws:account:111111111111",
                   empty=True)
        t.add_node("aws:vpc:vpc-00000001", "vpc", parent="aws:region:111111111111:us-east-1")
        return t

    def test_empty_regions_are_removed(self):
        out = filter_topology(self.topo(), hide_empty=True)
        self.assertNotIn("aws:region:111111111111:eu-west-1", out.nodes)
        self.assertIn("aws:region:111111111111:us-east-1", out.nodes)
        self.assertEqual(out.validate(), [])
        self.assertEqual(out.meta["filters"], [{"hide_empty": True}])

    def test_kept_by_default(self):
        out = filter_topology(self.topo())
        self.assertIn("aws:region:111111111111:eu-west-1", out.nodes)


class AiFilterTest(unittest.TestCase):
    def topo(self):
        from test_render import ai_topology
        return ai_topology()

    def test_models_view_keeps_the_org_chart_above_them(self):
        out = filter_topology(self.topo(), kinds=["bedrock-model"])
        kinds = {n.kind for n in out.nodes.values()}
        self.assertEqual(kinds, {"organization", "account", "region", "bedrock", "bedrock-model"})
        self.assertEqual(out.validate(), [])

    def test_iam_view_keeps_roles_users_and_trust_links(self):
        out = filter_topology(self.topo(), kinds=["iam-role", "iam-user"])
        self.assertEqual({n.kind for n in out.nodes.values()},
                         {"organization", "account", "iam", "iam-role", "iam-user"})
        self.assertEqual([l.kind for l in out.links.values()], ["iam-trust"])

    def test_hide_empty_keeps_regions_that_have_models(self):
        t = self.topo()
        t.add_node("aws:region:111122223333:eu-west-1", "region", "eu-west-1",
                   "aws:account:111122223333", empty=True)
        out = filter_topology(t, hide_empty=True)
        self.assertNotIn("aws:region:111122223333:eu-west-1", out.nodes)
        self.assertIn("aws:bedrock:111122223333:us-east-1", out.nodes)


if __name__ == "__main__":
    unittest.main()


class MaxPerTypeTest(unittest.TestCase):
    def inventory(self):
        t = Topology()
        t.add_node("aws:account:111122223333", "account", "111122223333")
        group = "aws:service:111122223333:us-east-1:acm"
        t.add_node(group, "aws-service", "Certificate Manager", "aws:account:111122223333",
                   service="acm")
        for i in range(5):
            arn = f"arn:aws:acm:us-east-1:111122223333:certificate/c{i}"
            t.add_node(f"aws:resource:{arn}", "aws-resource", f"c{i}", group, arn=arn,
                       service="acm", resource_type="AWS::CertificateManager::Certificate")
        t.add_node("aws:resource:arn:aws:acm-pca:us-east-1:111122223333:certificate-authority/ca",
                   "aws-resource", "ca", group, service="acm-pca",
                   resource_type="AWS::ACMPCA::CertificateAuthority")
        t.add_link("related", "aws:resource:arn:aws:acm:us-east-1:111122223333:certificate/c0",
                   "aws:resource:arn:aws:acm-pca:us-east-1:111122223333:certificate-authority/ca",
                   directed=True, relation="Is issued by")
        t.add_link("related", "aws:resource:arn:aws:acm:us-east-1:111122223333:certificate/c1",
                   "aws:resource:arn:aws:acm-pca:us-east-1:111122223333:certificate-authority/ca",
                   directed=True, relation="Is issued by")
        return t

    def test_types_over_the_limit_fold_into_one_counted_node(self):
        out = filter_topology(self.inventory(), max_per_type=4)
        self.assertEqual(out.validate(), [])
        summary = [n for n in out.by_kind("aws-resource") if n.attrs.get("summarized")]
        self.assertEqual(len(summary), 1)
        self.assertEqual(summary[0].label, "5 × certificate")
        self.assertEqual(summary[0].attrs["summarized"], 5)
        self.assertEqual(summary[0].parent, "aws:service:111122223333:us-east-1:acm")
        # The CA is under the limit and stays; the two links merge into one.
        self.assertEqual(len(out.by_kind("aws-resource")), 2)
        (link,) = out.links.values()
        self.assertEqual((link.source, link.attrs["collapsed_links"]), (summary[0].id, 2))
        self.assertEqual(out.meta["filters"], [{"max_per_type": 4}])

    def test_at_the_limit_nothing_folds(self):
        out = filter_topology(self.inventory(), max_per_type=5)
        self.assertEqual(len(out.by_kind("aws-resource")), 6)

    def test_bad_limit(self):
        with self.assertRaises(ValueError):
            filter_topology(self.inventory(), max_per_type=0)
