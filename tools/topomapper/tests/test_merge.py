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
ORG = os.path.join(FIXTURES, "org.json")   # the organization aws.json's account belongs to
PROD = "aws:account:123456789012"
OU = "aws:ou:ou-0000-prod0001"


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
        self.aws, self.org = Topology.load(AWS), Topology.load(ORG)

    def test_union_puts_the_account_in_its_ou(self):
        m = merge([self.org, self.aws])
        self.assertEqual(m.validate(), [])
        self.assertEqual(m.nodes[PROD].parent, OU)
        self.assertEqual(m.nodes["aws:region:123456789012:us-east-1"].parent, PROD)
        self.assertEqual(len(m.meta["sources"]), 2)
        shared = set(self.aws.nodes) & set(self.org.nodes)
        self.assertEqual(shared, {PROD})
        self.assertEqual(len(m.nodes), len(self.aws.nodes) + len(self.org.nodes) - 1)
        self.assertEqual(sorted(m.links), sorted(list(self.aws.links) + list(self.org.links)))
        self.assertEqual(m.nodes[PROD].attrs["path"], "Root/Prod")  # attributes from both
        self.assertEqual(m.meta.get("warnings", []), [])

    def test_merge_is_idempotent(self):
        once = merge([self.aws, self.org])
        twice = merge([once, self.org])
        self.assertEqual(sorted(twice.links), sorted(once.links))
        self.assertEqual(sorted(twice.nodes), sorted(once.nodes))
        self.assertEqual(twice.validate(), [])

    def test_kind_conflict_warns(self):
        other = Topology()
        other.add_node(OU, "account")
        m = merge([self.org, other])
        self.assertIn("is a ou in one input and a account", "\n".join(m.meta["warnings"]))

    def test_parent_conflict_warns(self):
        other = Topology()
        other.add_node("aws:region:999999999999:us-east-1", "region", "us-east-1")
        other.add_node("aws:vpc:vpc-0a1b2c3d4e5f60001", "vpc",
                       parent="aws:region:999999999999:us-east-1")
        m = merge([self.aws, other])
        self.assertIn("is inside aws:region:123456789012:us-east-1 in one input",
                      "\n".join(m.meta["warnings"]))

    def test_find_node(self):
        m = merge([self.aws, self.org])
        self.assertEqual(find_node(m, "Prod"), OU)
        self.assertEqual(find_node(m, OU), OU)
        m.add_node("aws:ou:ou-0000-prod0002", "ou", "Prod")
        with self.assertRaisesRegex(ValueError, "several nodes"):
            find_node(m, "Prod")
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
        status, out, err = run_cli(["topology", "merge", ORG, AWS])
        self.assertEqual(status, 0, err)
        m = Topology.loads(out)
        self.assertEqual(m.validate(), [])
        self.assertEqual(m.nodes[PROD].parent, OU)

    def test_cli_reads_stdin(self):
        with open(ORG, encoding="utf-8") as f:
            org = f.read()
        status, out, _ = run_cli(["topology", "merge", "-", AWS], stdin=org)
        self.assertEqual(status, 0)
        self.assertEqual(Topology.loads(out).nodes[PROD].parent, OU)

    def test_cli_errors(self):
        status, _, err = run_cli(["topology", "merge", os.path.join(FIXTURES, "does-not-exist.json")])
        self.assertEqual(status, 2)
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as exit_:
            cli.main(["topology", "merge", AWS, "--no-such-flag"])
        self.assertEqual(exit_.exception.code, 2)
        self.assertIn("unrecognized arguments: --no-such-flag", err.getvalue())


if __name__ == "__main__":
    unittest.main()
