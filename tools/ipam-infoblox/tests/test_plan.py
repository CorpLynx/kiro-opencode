import ipaddress
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ipam_infoblox.infoblox_csv import render, sections  # noqa: E402
from ipam_infoblox.plan import Options, build_plan, comment_for, render_template  # noqa: E402
from ipam_infoblox.records import Record  # noqa: E402
from ipam_infoblox.wapi import Existing  # noqa: E402

A, B = "111122223333", "123456789012"


def rec(cidr, rtype, rid="", name="", account=A, region="us-east-1", row=None):
    rec.row = getattr(rec, "row", 1) + 1
    return Record(row=row or rec.row, cidr=ipaddress.ip_network(cidr), resource_type=rtype,
                  resource_id=rid, name=name, account=account, region=region)


def grid(objects, views=("default",), eas=None):
    existing = Existing(views=list(views), ea_defs=eas or {})
    for view, cidr, kind in objects:
        existing.objects.setdefault(view, {})[ipaddress.ip_network(cidr)] = {
            "kind": kind, "comment": "", "extattrs": {}}
    return existing


def entries(plan):
    return {(e.view, str(e.cidr)): e for e in plan.entries}


class OfflineTest(unittest.TestCase):
    def test_pools_and_vpcs_are_containers_and_subnets_networks(self):
        plan = build_plan([rec("10.0.0.0/8", "pool", "ipam-pool-1"),
                           rec("10.0.0.0/16", "vpc", "vpc-1", "prod"),
                           rec("10.0.1.0/24", "subnet", "subnet-1", "app")], Options())
        found = entries(plan)
        self.assertEqual(found[("default", "10.0.0.0/8")].kind, "container")
        self.assertEqual(found[("default", "10.0.0.0/16")].kind, "container")
        app = found[("default", "10.0.1.0/24")]
        self.assertEqual((app.kind, app.status, app.parent),
                         ("network", "new", "10.0.0.0/16 (from this export)"))
        self.assertFalse(plan.blocked)

    def test_a_range_holding_others_becomes_a_container(self):
        plan = build_plan([rec("10.5.0.0/16", "allocation", "x"),
                           rec("10.5.1.0/24", "subnet", "subnet-1")], Options())
        holder = entries(plan)[("default", "10.5.0.0/16")]
        self.assertEqual(holder.kind, "container")
        self.assertIn("other ranges in the export sit inside it", holder.notes[0])

    def test_same_range_twice_is_merged(self):
        plan = build_plan([rec("10.0.0.0/16", "pool", "ipam-pool-2"),
                           rec("10.0.0.0/16", "vpc", "vpc-1", "prod")], Options())
        (entry,) = plan.entries
        self.assertEqual((entry.status, entry.primary.resource_id), ("new", "vpc-1"))
        self.assertIn("merged with the pool", entry.notes[0])

    def test_overlapping_vpcs_conflict_until_views_split_them(self):
        records = [rec("10.0.0.0/16", "vpc", "vpc-1", account=A),
                   rec("10.0.0.0/16", "vpc", "vpc-2", account=B)]
        (entry,) = build_plan(records, Options()).entries
        self.assertEqual(entry.status, "conflict")
        self.assertIn("--network-view 'aws-{account}'", entry.reasons[0])
        split = build_plan(records, Options(view="aws-{account}"))
        self.assertEqual(sorted(e.view for e in split.entries), [f"aws-{A}", f"aws-{B}"])
        self.assertFalse(split.blocked)

    def test_a_subnet_covering_its_vpc_is_a_network(self):
        plan = build_plan([rec("10.5.0.0/24", "vpc", "vpc-5"), rec("10.5.0.0/24", "subnet", "s5")],
                          Options())
        (entry,) = plan.entries
        self.assertEqual((entry.kind, entry.primary.resource_id), ("network", "s5"))
        self.assertIn("a subnet covers the whole range", entry.notes[0])
        # ...unless other ranges sit inside it.
        plan = build_plan([rec("10.6.0.0/24", "vpc", "vpc-6"), rec("10.6.0.0/24", "subnet", "s6"),
                           rec("10.6.0.0/28", "allocation", "a")], Options())
        self.assertEqual(entries(plan)[("default", "10.6.0.0/24")].kind, "container")

    def test_view_template_with_a_blank_field_leaves_the_row_out(self):
        plan = build_plan([rec("10.0.0.0/8", "pool", "ipam-pool-1", account=""),
                           rec("10.1.0.0/16", "vpc", "vpc-1")], Options(view="aws-{account}"))
        self.assertEqual([e.view for e in plan.entries], [f"aws-{A}"])
        self.assertIn("needs {account}, which this row doesn't have", plan.issues[0].message)

    def test_parent_left_out_is_said_so(self):
        plan = build_plan([rec("10.0.0.0/16", "vpc", "vpc-1", account=A),
                           rec("10.0.0.0/16", "vpc", "vpc-2", account=B),
                           rec("10.0.1.0/24", "subnet", "s1", account=A)], Options())
        child = entries(plan)[("default", "10.0.1.0/24")]
        self.assertEqual(child.status, "new")
        self.assertIn("but left out", child.parent)

    def test_comment_templates(self):
        record = rec("10.0.0.0/16", "vpc", "vpc-1")
        self.assertEqual(render_template("{name} ({resource_id})", record), "vpc-1")
        record.name = "prod"
        self.assertEqual(render_template("{name} ({resource_id})", record), "prod (vpc-1)")
        self.assertEqual(render_template("{name} [{region}] {column:Missing}", record),
                         "prod [us-east-1]")
        long = rec("10.1.0.0/16", "vpc", "vpc-2", "x" * 300)
        (entry,) = build_plan([long], Options()).entries
        self.assertEqual(len(comment_for(entry, Options())), 256)

    def test_import_file(self):
        plan = build_plan([rec("10.0.1.0/24", "subnet", "subnet-1", "app"),
                           rec("10.0.0.0/16", "vpc", "vpc-1", "prod"),
                           rec("10.0.0.0/8", "pool", "ipam-pool-1"),
                           rec("2001:db8:1000::/56", "vpc", "vpc-6"),
                           rec("2001:db8:1000:1::/64", "subnet", "subnet-6")],
                          Options(eas=[("AWS Account", "account"), ("Source", "text:AWS IPAM")]))
        self.assertEqual(render(sections(plan, Options(eas=[("AWS Account", "account"),
                                                              ("Source", "text:AWS IPAM")]))),
                         "header-networkcontainer,address*,netmask*,network_view,comment,"
                         "EA-AWS Account,EA-Source\r\n"
                         "networkcontainer,10.0.0.0,8,default,ipam-pool-1,111122223333,AWS IPAM\r\n"
                         "networkcontainer,10.0.0.0,16,default,prod (vpc-1),111122223333,AWS IPAM\r\n"
                         "header-network,address*,netmask*,network_view,comment,EA-AWS Account,"
                         "EA-Source\r\n"
                         "network,10.0.1.0,255.255.255.0,default,app (subnet-1),111122223333,AWS IPAM\r\n"
                         "header-ipv6networkcontainer,address*,cidr*,network_view,comment,"
                         "EA-AWS Account,EA-Source\r\n"
                         "ipv6networkcontainer,2001:db8:1000::,56,default,vpc-6,111122223333,AWS IPAM\r\n"
                         "header-ipv6network,address*,cidr*,network_view,comment,EA-AWS Account,"
                         "EA-Source\r\n"
                         "ipv6network,2001:db8:1000:1::,64,default,subnet-6,111122223333,AWS IPAM\r\n")
        dotted = render(sections(plan, Options(), "dotted", ("container",)))
        self.assertIn("networkcontainer,10.0.0.0,255.0.0.0,default", dotted)
        self.assertNotIn("header-network,", dotted)
        prefix = render(sections(plan, Options(), "prefix", ("network",)))
        self.assertIn("network,10.0.1.0,24,default", prefix)


class GridTest(unittest.TestCase):
    def test_existing_ranges_are_left_out(self):
        existing = grid([("default", "10.0.0.0/8", "container"), ("default", "10.0.1.0/24", "network")])
        plan = build_plan([rec("10.0.0.0/8", "pool", "p"), rec("10.0.1.0/24", "subnet", "s"),
                           rec("10.0.2.0/24", "subnet", "s2")], Options(), existing)
        found = entries(plan)
        self.assertEqual(found[("default", "10.0.0.0/8")].status, "exists")
        self.assertEqual(found[("default", "10.0.1.0/24")].status, "exists")
        new = found[("default", "10.0.2.0/24")]
        self.assertEqual((new.status, new.parent), ("new", "10.0.0.0/8 (already in Infoblox)"))
        self.assertFalse(plan.blocked)

    def test_inside_an_existing_network_is_a_conflict(self):
        existing = grid([("default", "10.3.0.0/16", "network")])
        plan = build_plan([rec("10.3.5.0/24", "subnet", "s")], Options(), existing)
        (entry,) = plan.entries
        self.assertEqual(entry.status, "conflict")
        self.assertIn("convert 10.3.0.0/16 to a network container", entry.reasons[0])
        self.assertTrue(plan.blocked)

    def test_holding_existing_networks_makes_a_container(self):
        existing = grid([("default", "10.7.1.0/24", "network")])
        plan = build_plan([rec("10.7.0.0/16", "allocation", "a")], Options(), existing)
        (entry,) = plan.entries
        self.assertEqual((entry.status, entry.kind), ("new", "container"))
        self.assertIn("Infoblox already has 1 range(s) inside it, such as 10.7.1.0/24",
                      entry.notes[0])

    def test_container_that_is_a_network_in_the_grid(self):
        existing = grid([("default", "10.8.0.0/16", "network")])
        plan = build_plan([rec("10.8.0.0/16", "vpc", "vpc-8"), rec("10.8.1.0/24", "subnet", "s")],
                          Options(), existing)
        found = entries(plan)
        self.assertEqual(found[("default", "10.8.0.0/16")].status, "conflict")
        self.assertEqual(found[("default", "10.8.1.0/24")].status, "conflict")  # inside a network
        lone = build_plan([rec("10.8.0.0/16", "vpc", "vpc-8")], Options(), existing)
        self.assertEqual(lone.entries[0].status, "exists")
        self.assertIn("Infoblox has it as a network", lone.entries[0].notes[0])

    def test_views_are_separate(self):
        existing = grid([("other", "10.0.0.0/16", "network")], views=("default", "other"))
        plan = build_plan([rec("10.0.0.0/16", "vpc", "vpc-1")], Options(), existing)
        self.assertEqual(plan.entries[0].status, "new")

    def test_missing_view_and_attributes(self):
        existing = grid([], views=("default",), eas={
            "AWS Account": {"type": "STRING", "values": []},
            "Environment": {"type": "ENUM", "values": ["prod", "dev"]}})
        records = [rec("10.0.0.0/16", "vpc", "vpc-1", region="us-east-1")]
        plan = build_plan(records, Options(view="aws-{account}", eas=[
            ("AWS Account", "account"), ("Environment", "region"), ("Owner", "text:net")]), existing)
        problems = " / ".join(plan.problems)
        self.assertIn(f"network view 'aws-{A}' doesn't exist in the Grid, so its 1 range(s) are "
                      "left out", problems)
        self.assertIn("extensible attribute 'Owner' isn't defined", problems)
        self.assertEqual(plan.entries[0].status, "conflict")  # not written to the import file
        self.assertTrue(plan.blocked)
        plan = build_plan(records, Options(eas=[("Environment", "region")]), existing)
        self.assertEqual(plan.problems, ["extensible attribute 'Environment' only allows prod, dev; "
                                         "the export has us-east-1"])


if __name__ == "__main__":
    unittest.main()
