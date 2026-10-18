import contextlib
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from topomap import cli  # noqa: E402
from topomap.merge import find_node, merge, read_topologies, read_topology  # noqa: E402
from topomap.model import Topology  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "merge")
AWS = os.path.join(FIXTURES, "aws.json")
NETWORK = os.path.join(FIXTURES, "network.json")

CGW_HQ = "aws:customer-gateway:cgw-0a1b2c3d4e5f60001"
CGW_NAT = "aws:customer-gateway:cgw-0a1b2c3d4e5f60002"
DXCON = "aws:dx-connection:dxcon-ffa1b2c3"
EDGE1, DXR1, BRANCH1 = "net:device:edge1", "net:device:dxr1", "net:device:branch1"


def run_cli(argv, stdin=""):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            mock.patch("sys.stdin", io.StringIO(stdin)):
        status = cli.main(argv)
    return status, out.getvalue(), err.getvalue()


def stitches(topo):
    return {l.id: l for l in topo.links.values() if l.kind == "stitch"}


def device_with(name, ip):
    """A network piece of one device holding `ip`."""
    t = Topology()
    t.add_node(f"net:device:{name}", "device", name,
               interfaces=[{"name": "GigabitEthernet0/0", "addresses": [f"{ip}/29"]}])
    return t


class UncollectedAccountsTest(unittest.TestCase):
    def accounts(self, *ids):
        t = Topology()
        for i in ids:
            t.add_node(f"aws:account:{i}", "account", i, account_id=i)
        return t

    def test_org_only_accounts_are_marked_when_others_were_collected(self):
        org = self.accounts("111111111111", "222222222222")
        collected = Topology()
        collected.add_node("aws:account:111111111111", "account", collected=True)
        m = merge([org, collected])
        self.assertIs(m.nodes["aws:account:111111111111"].attrs["collected"], True)
        self.assertIs(m.nodes["aws:account:222222222222"].attrs["collected"], False)

    def test_nothing_is_marked_without_a_collection(self):
        m = merge([self.accounts("111111111111")])
        self.assertNotIn("collected", m.nodes["aws:account:111111111111"].attrs)


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.aws, self.net = Topology.load(AWS), Topology.load(NETWORK)

    def test_union_and_cgw_ip_rule(self):
        m = merge([self.aws, self.net])
        self.assertEqual(m.validate(), [])
        self.assertIn("aws:vpc:vpc-0a1b2c3d4e5f60001", m.nodes)
        self.assertIn(EDGE1, m.nodes)
        self.assertEqual(len(m.meta["sources"]), 2)
        link = m.links["stitch|aws:customer-gateway:cgw-0a1b2c3d4e5f60001|net:device:edge1|203.0.113.10"]
        self.assertEqual((link.source, link.target), (CGW_HQ, EDGE1))
        self.assertEqual(link.label, "203.0.113.10")
        self.assertEqual(link.attrs["rule"], "cgw-ip")
        self.assertEqual(link.attrs["address"], "203.0.113.10")
        self.assertEqual(link.attrs["target_port"], "GigabitEthernet0/0/0")

    def test_dx_vif_rule_stitches_to_the_connection(self):
        m = merge([self.aws, self.net])
        link = m.links[f"stitch|{DXCON}|{DXR1}|172.16.255.1"]
        self.assertEqual(link.attrs["rule"], "dx-vif")
        self.assertEqual(link.attrs["vif_id"], "dxvif-ffa1b2c3")
        self.assertEqual(link.attrs["target_port"], "TenGigabitEthernet0/1/0.100")

    def test_summary_and_nat_warning(self):
        m = merge([self.aws, self.net])
        summary = m.meta["stitched"]
        self.assertTrue(summary["automatic"])
        self.assertEqual(summary["counts"], {"cgw-ip": 1, "dx-vif": 1, "gcp-vpn-peer": 0, "gcp-interconnect": 0, "manual": 0})
        self.assertEqual(summary["links"], sorted(stitches(m)))
        self.assertEqual(summary["unmatched_customer_gateways"], [CGW_NAT])
        warnings = "\n".join(m.meta["warnings"])
        self.assertIn("behind NAT? use --stitch", warnings)
        self.assertIn(CGW_NAT, warnings)
        self.assertNotIn(CGW_HQ, warnings)

    def test_no_nat_warning_without_any_device_addresses(self):
        m = merge([self.aws])
        self.assertEqual(stitches(m), {})
        self.assertNotIn("warnings", m.meta)

    def test_manual_pair_by_label_and_id(self):
        m = merge([self.aws, self.net], manual=["branch1=branch-nat (cgw-0a1b2c3d4e5f60002)"])
        link = m.links[f"stitch|{CGW_NAT}|{BRANCH1}|manual"]
        self.assertEqual(link.attrs["rule"], "manual")
        self.assertNotIn("address", link.attrs)
        self.assertEqual(m.meta["stitched"]["counts"]["manual"], 1)
        self.assertEqual(m.meta["stitched"]["unmatched_customer_gateways"], [])
        self.assertNotIn("behind NAT", "\n".join(m.meta.get("warnings", [])))
        again = merge([self.aws, self.net], manual=[(BRANCH1, CGW_NAT)])
        self.assertEqual(sorted(stitches(again)), sorted(stitches(m)))

    def test_manual_pair_already_stitched_is_not_doubled(self):
        m = merge([self.aws, self.net], manual=[f"{EDGE1}={CGW_HQ}"])
        self.assertEqual(len(stitches(m)), 2)
        self.assertEqual(m.meta["stitched"]["counts"]["manual"], 0)

    def test_manual_errors(self):
        for pair, message in ((f"{EDGE1}=nope", "no node with ID or label 'nope'"),
                              ("edge1", "expected NODE=NODE"),
                              (f"{EDGE1}=edge1", "both ends are")):
            with self.subTest(pair=pair), self.assertRaisesRegex(ValueError, message):
                merge([self.aws, self.net], manual=[pair])

    def test_ambiguous_address_stitches_all_and_warns(self):
        m = merge([self.aws, device_with("fw1", "203.0.113.10"), device_with("fw2", "203.0.113.10")])
        ends = sorted(l.target for l in stitches(m).values())
        self.assertEqual(ends, ["net:device:fw1", "net:device:fw2"])
        self.assertIn("is on several devices", "\n".join(m.meta["warnings"]))
        self.assertEqual(m.validate(), [])

    def test_no_stitch_skips_automatic_rules_but_keeps_manual(self):
        m = merge([self.aws, self.net], stitch=False)
        self.assertEqual(stitches(m), {})
        self.assertFalse(m.meta["stitched"]["automatic"])
        self.assertNotIn("behind NAT", "\n".join(m.meta.get("warnings", [])))
        m = merge([self.aws, self.net], stitch=False, manual=[f"{BRANCH1}={CGW_NAT}"])
        self.assertEqual([l.attrs["rule"] for l in stitches(m).values()], ["manual"])

    def test_ipv6_addresses_match(self):
        aws = Topology()
        aws.add_node("aws:customer-gateway:cgw-6", "customer-gateway", ip="2001:db8::a")
        net = Topology()
        net.add_node("net:device:r6", "device",
                     interfaces=[{"name": "Gi0/0", "addresses": ["2001:DB8:0::A/64"]}])
        self.assertEqual(len(stitches(merge([aws, net]))), 1)

    def test_merge_is_idempotent(self):
        once = merge([self.aws, self.net])
        twice = merge([once, self.net])
        self.assertEqual(sorted(twice.links), sorted(once.links))
        self.assertEqual(twice.validate(), [])

    def test_kind_conflict_warns(self):
        other = Topology()
        other.add_node(EDGE1, "segment")
        m = merge([self.net, other])
        self.assertIn("is a device in one input and a segment", "\n".join(m.meta["warnings"]))

    def test_find_node(self):
        m = merge([self.aws, self.net])
        self.assertEqual(find_node(m, "edge1"), EDGE1)
        self.assertEqual(find_node(m, EDGE1), EDGE1)
        m.add_node("net:device:edge1-copy", "device", "edge1")
        with self.assertRaisesRegex(ValueError, "several nodes"):
            find_node(m, "edge1")

    def test_read_errors_name_the_path(self):
        with self.assertRaisesRegex(ValueError, "only be read once"):
            read_topologies(["-", "-"])
        with mock.patch("sys.stdin", io.StringIO('{"schema": "topomap/1", "nodes": [{}]}')):
            with self.assertRaisesRegex(ValueError, "^-: not a valid topology"):
                read_topology("-")


class MergeCliTest(unittest.TestCase):
    def test_cli_merges_and_reports_warnings(self):
        status, out, err = run_cli(["merge", AWS, NETWORK])
        self.assertEqual(status, 0)
        m = Topology.loads(out)
        self.assertEqual(m.validate(), [])
        self.assertEqual(len(stitches(m)), 2)
        self.assertIn("warning: customer gateway branch-nat", err)

    def test_cli_stdin_manual_and_no_stitch(self):
        with open(NETWORK, encoding="utf-8") as f:
            net = f.read()
        status, out, _ = run_cli(["merge", AWS, "-", "--no-stitch",
                                  "--stitch", f"branch1={CGW_NAT}"], stdin=net)
        self.assertEqual(status, 0)
        m = Topology.loads(out)
        self.assertEqual([l.attrs["rule"] for l in stitches(m).values()], ["manual"])

    def test_cli_errors(self):
        status, _, err = run_cli(["merge", AWS, NETWORK, "--stitch", "edge1=missing"])
        self.assertEqual(status, 2)
        self.assertIn("no node with ID or label 'missing'", err)
        status, _, err = run_cli(["merge", os.path.join(FIXTURES, "does-not-exist.json")])
        self.assertEqual(status, 2)


if __name__ == "__main__":
    unittest.main()
