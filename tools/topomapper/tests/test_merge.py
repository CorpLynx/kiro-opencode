import contextlib
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from topomapper import cli  # noqa: E402
from topomapper.merge import find_node, merge, read_topologies, read_topology  # noqa: E402
from topomapper.model import Topology  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "merge")
AWS = os.path.join(FIXTURES, "aws.json")
LAN = os.path.join(FIXTURES, "lan.json")   # topomapper scan of the tests/fixtures/scan captures
GW = "net:device:gw"


def run_cli(argv, stdin=""):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            mock.patch("sys.stdin", io.StringIO(stdin)):
        status = cli.main(argv)
    return status, out.getvalue(), err.getvalue()


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
        self.aws, self.lan = Topology.load(AWS), Topology.load(LAN)

    def test_union(self):
        m = merge([self.aws, self.lan])
        self.assertEqual(m.validate(), [])
        self.assertIn("aws:vpc:vpc-0a1b2c3d4e5f60001", m.nodes)
        self.assertIn(GW, m.nodes)
        self.assertEqual(len(m.meta["sources"]), 2)
        self.assertEqual(len(m.nodes), len(self.aws.nodes) + len(self.lan.nodes))
        self.assertEqual(sorted(m.links), sorted(list(self.aws.links) + list(self.lan.links)))

    def test_merge_is_idempotent(self):
        once = merge([self.aws, self.lan])
        twice = merge([once, self.lan])
        self.assertEqual(sorted(twice.links), sorted(once.links))
        self.assertEqual(sorted(twice.nodes), sorted(once.nodes))
        self.assertEqual(twice.validate(), [])

    def test_kind_conflict_warns(self):
        other = Topology()
        other.add_node(GW, "segment")
        m = merge([self.lan, other])
        self.assertIn("is a device in one input and a segment", "\n".join(m.meta["warnings"]))

    def test_parent_conflict_warns(self):
        other = Topology()
        other.add_node("aws:region:999999999999:us-east-1", "region", "us-east-1")
        other.add_node("aws:vpc:vpc-0a1b2c3d4e5f60001", "vpc",
                       parent="aws:region:999999999999:us-east-1")
        m = merge([self.aws, other])
        self.assertIn("is inside aws:region:123456789012:us-east-1 in one input",
                      "\n".join(m.meta["warnings"]))

    def test_find_node(self):
        m = merge([self.aws, self.lan])
        self.assertEqual(find_node(m, "gw"), GW)
        self.assertEqual(find_node(m, GW), GW)
        m.add_node("net:device:gw-copy", "device", "gw")
        with self.assertRaisesRegex(ValueError, "several nodes"):
            find_node(m, "gw")
        with self.assertRaisesRegex(ValueError, "no node with ID or label 'nope'"):
            find_node(m, "nope")

    def test_read_errors_name_the_path(self):
        with self.assertRaisesRegex(ValueError, "only be read once"):
            read_topologies(["-", "-"])
        with mock.patch("sys.stdin", io.StringIO('{"schema": "topomap/1", "nodes": [{}]}')):
            with self.assertRaisesRegex(ValueError, "^-: not a valid topology"):
                read_topology("-")


class MergeCliTest(unittest.TestCase):
    def test_cli_merges(self):
        status, out, err = run_cli(["merge", AWS, LAN])
        self.assertEqual(status, 0, err)
        m = Topology.loads(out)
        self.assertEqual(m.validate(), [])
        self.assertIn(GW, m.nodes)

    def test_cli_reads_stdin(self):
        with open(LAN, encoding="utf-8") as f:
            lan = f.read()
        status, out, _ = run_cli(["merge", AWS, "-"], stdin=lan)
        self.assertEqual(status, 0)
        self.assertIn(GW, Topology.loads(out).nodes)

    def test_cli_errors(self):
        status, _, err = run_cli(["merge", os.path.join(FIXTURES, "does-not-exist.json")])
        self.assertEqual(status, 2)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as exit_:
            cli.main(["merge", AWS, "--no-such-flag"])
        self.assertEqual(exit_.exception.code, 2)
        self.assertIn("unrecognized arguments: --no-such-flag", err.getvalue())


if __name__ == "__main__":
    unittest.main()
