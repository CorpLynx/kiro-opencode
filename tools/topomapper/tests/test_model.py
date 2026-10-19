import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from topomapper.model import (LINK_VPC_PEERING, SCHEMA, Topology, link_id, node_id,  # noqa: E402
                           union)


class ModelTest(unittest.TestCase):
    def build(self):
        t = Topology({"collector": "test"})
        t.add_node("aws:account:123456789012", "account", "acct")
        t.add_node("aws:region:123456789012:us-east-1", "region", "us-east-1",
                   "aws:account:123456789012")
        t.add_node("aws:vpc:vpc-1", "vpc", "prod (vpc-1)", "aws:region:123456789012:us-east-1",
                   cidrs=["10.0.0.0/16"])
        return t

    def test_ids_are_deterministic(self):
        self.assertEqual(node_id("aws", "vpc", "vpc-1"), "aws:vpc:vpc-1")
        self.assertEqual(link_id("vpc-peering", "b", "a", "p2", "p1"),
                         link_id("vpc-peering", "a", "b", "p1", "p2"))
        self.assertNotEqual(link_id("vpn", "a", "b", directed=True),
                            link_id("vpn", "b", "a", directed=True))

    def test_add_node_fills_in(self):
        t = self.build()
        t.add_node("aws:vpc:vpc-1", "vpc", cidrs=["10.1.0.0/16"], is_default=False)
        vpc = t.nodes["aws:vpc:vpc-1"]
        self.assertEqual(vpc.attrs["cidrs"], ["10.0.0.0/16", "10.1.0.0/16"])
        self.assertIs(vpc.attrs["is_default"], False)
        self.assertEqual(vpc.label, "prod (vpc-1)")

    def test_links_are_canonical(self):
        t = Topology()
        for n in ("net:device:a", "net:device:b"):
            t.add_node(n, "device")
        one = t.add_link(LINK_VPC_PEERING, "net:device:b", "net:device:a",
                         source_port="eth2", target_port="eth1", sources=["scan-1.xml"])
        two = t.add_link(LINK_VPC_PEERING, "net:device:a", "net:device:b",
                         source_port="eth1", target_port="eth2", sources=["scan-2.xml"])
        self.assertIs(one, two)
        self.assertEqual((one.source, one.attrs["source_port"]), ("net:device:a", "eth1"))
        self.assertEqual(one.attrs["sources"], ["scan-1.xml", "scan-2.xml"])

    def test_round_trip_and_sorted(self):
        t = self.build()
        text = t.dumps()
        self.assertEqual(Topology.loads(text).dumps(), text)
        self.assertIn(f'"schema": "{SCHEMA}"', text)
        with self.assertRaises(ValueError):
            Topology.loads('{"schema": "other/9"}')

    def test_validate(self):
        t = self.build()
        self.assertEqual(t.validate(), [])
        t.add_node("aws:subnet:s-1", "subnet", parent="aws:vpc:missing")
        t.add_link("vpc-peering", "aws:vpc:vpc-1", "aws:vpc:nope")
        t.add_node("x", "spaceship")
        problems = "\n".join(t.validate())
        for expected in ("parent aws:vpc:missing", "endpoint aws:vpc:nope", "unknown kind 'spaceship'"):
            self.assertIn(expected, problems)

    def test_tree_helpers(self):
        t = self.build()
        self.assertEqual([n.id for n in t.children(None)], ["aws:account:123456789012"])
        self.assertEqual(t.ancestors("aws:vpc:vpc-1"),
                         ["aws:region:123456789012:us-east-1", "aws:account:123456789012"])

    def test_union_and_warnings(self):
        a, b = self.build(), Topology({"collector": "other"})
        b.add_node("aws:vpc:vpc-1", "vpc", tags={"env": "prod"})
        b.warn("region eu-west-1: AccessDenied")
        u = union([a, b])
        self.assertEqual(u.nodes["aws:vpc:vpc-1"].attrs["tags"], {"env": "prod"})
        self.assertEqual(len(u.meta["sources"]), 2)
        self.assertEqual(u.meta["warnings"], ["region eu-west-1: AccessDenied"])


if __name__ == "__main__":
    unittest.main()
