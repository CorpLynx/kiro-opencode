import ipaddress
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ipam_infoblox.reader import Table, map_columns  # noqa: E402
from ipam_infoblox.records import load_records, normalize_type, parse_cidrs  # noqa: E402

HEADERS = ["Resource ID", "Resource name", "Resource type", "Resource CIDR", "Resource owner"]


def table(*rows):
    t = Table("export.csv", "", 1, list(HEADERS))
    for i, row in enumerate(rows, start=2):
        t.rows.append((i, dict(zip(HEADERS, row))))
    map_columns(t)
    return t


class RecordsTest(unittest.TestCase):
    def test_cleaning_and_issues(self):
        records, issues = load_records(table(
            ["vpc-1", "prod", "VPC", "10.0.0.0/16", "111122223333"],
            ["vpc-2", "-", "VPC", "10.1.0.0/16; 10.2.0.0/16", "n/a"],
            ["subnet-1", "app", "Subnet", "10.1.1.9/24", "111122223333"],
            ["eipalloc-1", "nat", "Elastic IP", "198.51.100.10/32", "111122223333"],
            ["subnet-2", "x", "Subnet", "10.4.0.0", "111122223333"],
            ["subnet-3", "y", "Subnet", "not-a-cidr/24", "111122223333"],
            ["subnet-4", "z", "Subnet", "None", "111122223333"],
            ["vpc-1", "prod", "VPC", "10.0.0.0/16", "111122223333"],
        ))
        self.assertEqual([str(r.cidr) for r in records],
                         ["10.0.0.0/16", "10.1.0.0/16", "10.2.0.0/16", "10.1.1.0/24"])
        self.assertEqual((records[1].name, records[1].account), ("", ""))  # "-" and "n/a" are blank
        self.assertEqual(records[0].resource_type, "vpc")
        messages = [(i.row, i.action, i.message) for i in issues]
        self.assertIn((3, "changed", "2 CIDRs in one cell, split into one network each"), messages)
        self.assertIn((4, "changed", "host bits cleared: now 10.1.1.0/24"), messages)
        self.assertIn((5, "dropped", "eip is an address, not a network (keep with --keep-type eip)"),
                      messages)
        self.assertIn((6, "dropped", "'10.4.0.0' has no prefix length"), messages)
        self.assertIn((7, "dropped", "'not-a-cidr/24' is not a CIDR"), messages)
        self.assertIn((8, "dropped", "no CIDR"), messages)
        self.assertIn((9, "dropped", "repeats an earlier row for 10.0.0.0/16"), messages)

    def test_words_beside_a_cidr_are_ignored_not_fatal(self):
        records, issues = load_records(table(["vpc-1", "prod", "VPC", "10.0.0.0/16 (primary)", ""]))
        self.assertEqual([str(r.cidr) for r in records], ["10.0.0.0/16"])
        self.assertEqual([(i.action, i.message) for i in issues],
                         [("changed", "ignored '(primary)' has no prefix length")])

    def test_keep_and_drop_types(self):
        rows = (["eipalloc-1", "nat", "EIP", "198.51.100.10/32", ""],
                ["ipam-pool-1", "top", "Pool", "10.0.0.0/8", ""])
        records, _ = load_records(table(*rows), keep_types=["eip"], drop_types=["pool"])
        self.assertEqual([r.resource_type for r in records], ["eip"])

    def test_types_and_cidrs(self):
        self.assertEqual(normalize_type("Public IPv4 pool"), "public-ipv4-pool")
        self.assertEqual(normalize_type("Network Interface"), "eni")
        self.assertEqual(normalize_type("Transit Gateway"), "transit-gateway")
        cidrs, problems = parse_cidrs("10.0.0.0/16,2001:db8::/32\n10.9.9.9")
        self.assertEqual(cidrs, [ipaddress.ip_network("10.0.0.0/16"),
                                 ipaddress.ip_network("2001:db8::/32")])
        self.assertEqual(problems, ["'10.9.9.9' has no prefix length"])

    def test_values_for_templates(self):
        records, _ = load_records(table(["vpc-1", "prod", "VPC", "10.0.0.0/16", "111122223333"]))
        record = records[0]
        self.assertEqual(record.value("account"), "111122223333")
        self.assertEqual(record.value("cidr"), "10.0.0.0/16")
        self.assertEqual(record.value("column:Resource name"), "prod")
        self.assertEqual(record.value("text:AWS"), "AWS")


if __name__ == "__main__":
    unittest.main()
