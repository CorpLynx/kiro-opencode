"""--from-aws, against botocore's Stubber: every request and answer is checked
against the real EC2 API model, and no call leaves the process."""

import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from ipam_infoblox import aws_source, cli  # noqa: E402
from ipam_infoblox.plan import Options, build_plan  # noqa: E402
from ipam_infoblox.reader import map_columns, read_table  # noqa: E402
from ipam_infoblox.records import load_records  # noqa: E402

try:
    import boto3
    from botocore.stub import Stubber
except ImportError:  # pragma: no cover - optional dependency
    boto3 = None

REGION, A, B = "us-east-1", "111122223333", "123456789012"
IPAM = f"arn:aws:ec2::{A}:ipam/ipam-0a1b2c3d4e5f60001"
PRIVATE = f"arn:aws:ec2::{A}:ipam-scope/ipam-scope-0a1b2c3d4e5f60001"
PUBLIC = f"arn:aws:ec2::{A}:ipam-scope/ipam-scope-0a1b2c3d4e5f60002"


def ipam(n=1):
    return {"IpamId": f"ipam-0a1b2c3d4e5f6000{n}", "IpamArn": IPAM, "IpamRegion": REGION,
            "OwnerId": A, "PrivateDefaultScopeId": "ipam-scope-0a1b2c3d4e5f60001",
            "PublicDefaultScopeId": "ipam-scope-0a1b2c3d4e5f60002"}


SCOPES = {"IpamScopes": [
    {"IpamScopeId": "ipam-scope-0a1b2c3d4e5f60001", "IpamScopeArn": PRIVATE, "IpamArn": IPAM,
     "IpamScopeType": "private", "IsDefault": True},
    {"IpamScopeId": "ipam-scope-0a1b2c3d4e5f60002", "IpamScopeArn": PUBLIC, "IpamArn": IPAM,
     "IpamScopeType": "public", "IsDefault": True}]}
POOLS = {"IpamPools": [
    {"IpamPoolId": "ipam-pool-0a1b2c3d4e5f60001", "IpamScopeArn": PRIVATE, "OwnerId": A,
     "Locale": "None", "State": "create-complete", "Description": "everything",
     "Tags": [{"Key": "Name", "Value": "top"}]},
    {"IpamPoolId": "ipam-pool-0a1b2c3d4e5f60002", "IpamScopeArn": PRIVATE, "OwnerId": A,
     "Locale": REGION, "State": "create-complete", "SourceIpamPoolId": "ipam-pool-0a1b2c3d4e5f60001"},
    {"IpamPoolId": "ipam-pool-0a1b2c3d4e5f60003", "IpamScopeArn": PRIVATE, "OwnerId": A,
     "State": "delete-complete"},
    {"IpamPoolId": "ipam-pool-0a1b2c3d4e5f60004", "IpamScopeArn": PUBLIC, "OwnerId": A,
     "State": "create-complete"}]}
POOL_CIDRS = {
    "ipam-pool-0a1b2c3d4e5f60001": [{"Cidr": "10.0.0.0/8", "State": "provisioned"},
                                    {"Cidr": "172.16.0.0/12", "State": "deprovisioned"}],
    "ipam-pool-0a1b2c3d4e5f60002": [{"Cidr": "10.0.0.0/12", "State": "provisioned"}],
}
ALLOCATIONS = {
    "ipam-pool-0a1b2c3d4e5f60001": [{"Cidr": "10.0.0.0/12", "ResourceType": "ipam-pool",
                                     "ResourceId": "ipam-pool-0a1b2c3d4e5f60002"}],
    "ipam-pool-0a1b2c3d4e5f60002": [{"Cidr": "10.15.0.0/16", "ResourceType": "custom",
                                     "IpamPoolAllocationId": "ipam-pool-alloc-0a1b2c3d4e5f60001",
                                     "Description": "on-prem DC reservation"}],
}


def allocation_tags():
    """Whether this botocore's EC2 model has tags on pool allocations (newer
    ones do); answers must match the model, so older ones get none."""
    model = boto3.client("ec2", region_name=REGION).meta.service_model
    shape = model.operation_model("GetIpamPoolAllocations").output_shape
    return "Tags" in shape.members["IpamPoolAllocations"].member.members
RESOURCES = {"IpamResourceCidrs": [
    {"ResourceId": "vpc-0a1b2c3d4e5f60001", "ResourceType": "vpc", "ResourceCidr": "10.0.0.0/16",
     "ResourceRegion": REGION, "ResourceOwnerId": A, "ResourceName": "prod",
     "IpamPoolId": "ipam-pool-0a1b2c3d4e5f60002", "ManagementState": "managed",
     "ComplianceStatus": "compliant", "OverlapStatus": "nonoverlapping", "IpUsage": 0.25,
     "ResourceTags": [{"Key": "Name", "Value": "prod"}, {"Key": "Env", "Value": "prod"}]},
    {"ResourceId": "subnet-0a1b2c3d4e5f60001", "ResourceType": "subnet",
     "ResourceCidr": "10.0.1.0/24", "ResourceRegion": REGION, "ResourceOwnerId": A,
     "VpcId": "vpc-0a1b2c3d4e5f60001", "ManagementState": "managed",
     "ResourceTags": [{"Key": "Name", "Value": "app-a"}]},
    {"ResourceId": "vpc-0a1b2c3d4e5f60002", "ResourceType": "vpc", "ResourceCidr": "10.0.0.0/16",
     "ResourceRegion": "us-west-2", "ResourceOwnerId": B, "ManagementState": "unmanaged",
     "OverlapStatus": "overlapping"},
    {"ResourceId": "eipalloc-0a1b2c3d4e5f60001", "ResourceType": "eip",
     "ResourceCidr": "198.51.100.10/32", "ResourceRegion": REGION, "ResourceOwnerId": A},
]}


def stub_ipam(client, ipams=None, resources=RESOURCES):
    """Queue the answers one --from-aws run asks for, in order."""
    stub = Stubber(client)
    stub.add_response("describe_ipams", {"Ipams": ipams or [ipam()]})
    if ipams and len(ipams) > 1:
        stub.activate()
        return stub
    stub.add_response("describe_ipam_scopes", SCOPES)
    stub.add_response("describe_ipam_pools", POOLS)
    for pool in ("ipam-pool-0a1b2c3d4e5f60001", "ipam-pool-0a1b2c3d4e5f60002"):
        stub.add_response("get_ipam_pool_cidrs", {"IpamPoolCidrs": POOL_CIDRS[pool]},
                          {"IpamPoolId": pool})
        allocations = [dict(a) for a in ALLOCATIONS[pool]]
        for allocation in allocations:
            if allocation["ResourceType"] == "custom" and allocation_tags():
                allocation["Tags"] = [{"Key": "Owner", "Value": "netops"}]
        stub.add_response("get_ipam_pool_allocations", {"IpamPoolAllocations": allocations},
                          {"IpamPoolId": pool})
    stub.add_response("get_ipam_resource_cidrs", resources,
                      {"IpamScopeId": "ipam-scope-0a1b2c3d4e5f60001"})
    stub.activate()
    return stub


@unittest.skipIf(boto3 is None, "needs boto3")
class FromAwsTest(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, {"AWS_ACCESS_KEY_ID": "testing",
                                           "AWS_SECRET_ACCESS_KEY": "testing",
                                           "AWS_CONFIG_FILE": os.devnull,
                                           "AWS_SHARED_CREDENTIALS_FILE": os.devnull})
        env.start()
        self.addCleanup(env.stop)
        self.raw = boto3.client("ec2", region_name=REGION)
        self.client = aws_source.ReadOnlyClient(self.raw)

    def test_the_ipam_becomes_a_table(self):
        stub = stub_ipam(self.raw)
        table = aws_source.fetch(self.client)
        stub.assert_no_pending_responses()
        rows = [values for _, values in table.rows]
        self.assertEqual([(r["Resource type"], r["Resource CIDR"]) for r in rows], [
            ("pool", "10.0.0.0/8"), ("pool", "10.0.0.0/12"), ("allocation", "10.15.0.0/16"),
            ("eip", "198.51.100.10/32"), ("subnet", "10.0.1.0/24"), ("vpc", "10.0.0.0/16"),
            ("vpc", "10.0.0.0/16")])
        top, _, custom, _, _, prod, _ = rows
        self.assertEqual((top["Resource name"], top["Region"]), ("top", "None"))
        self.assertEqual((custom["Resource name"], custom["IPAM pool ID"]),
                         ("on-prem DC reservation", "ipam-pool-0a1b2c3d4e5f60002"))
        self.assertEqual(custom.get("tag:Owner", ""), "netops" if allocation_tags() else "")
        self.assertEqual((prod["IP usage"], prod["tag:Env"], prod["Resource owner"]), ("25%", "prod", A))
        self.assertEqual(table.headers[len(aws_source.HEADERS):],
                         ["tag:Env", "tag:Name"] + (["tag:Owner"] if allocation_tags() else []))
        self.assertEqual([n for n, _ in table.rows], list(range(2, 9)))
        self.assertIn("ipam-0a1b2c3d4e5f60001 (us-east-1), scope ipam-scope-0a1b2c3d4e5f60001",
                      table.path)

    def test_the_table_plans_like_an_export(self):
        stub_ipam(self.raw)
        table = aws_source.fetch(self.client)
        map_columns(table)
        records, issues = load_records(table)
        self.assertEqual([i.message for i in issues],
                         ["eip is an address, not a network (keep with --keep-type eip)"])
        plan = build_plan(records, Options(view="aws-{account}"))
        kinds = {(e.view, str(e.cidr)): (e.kind, e.status) for e in plan.entries}
        self.assertEqual(kinds[(f"aws-{A}", "10.0.0.0/8")], ("container", "new"))
        self.assertEqual(kinds[(f"aws-{A}", "10.15.0.0/16")], ("network", "new"))
        self.assertEqual(kinds[(f"aws-{A}", "10.0.1.0/24")], ("network", "new"))
        # The overlapping VPC in the other account gets a view of its own.
        self.assertEqual(kinds[(f"aws-{B}", "10.0.0.0/16")], ("container", "new"))

    def test_saved_export_reads_back_the_same(self):
        stub_ipam(self.raw)
        table = aws_source.fetch(self.client)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ipam.csv")
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(aws_source.to_csv(table))
            again = read_table(path)
        self.assertEqual(again.headers, table.headers)
        self.assertEqual(again.rows, table.rows)  # same row numbers, so the report points into it

    def fresh(self):
        raw = boto3.client("ec2", region_name=REGION)
        return raw, aws_source.ReadOnlyClient(raw)

    def test_choosing_the_ipam_and_scope(self):
        stub_ipam(self.raw, ipams=[ipam(1), ipam(2)])
        with self.assertRaisesRegex(ValueError, "2 IPAMs in us-east-1 .*--ipam-id"):
            aws_source.fetch(self.client)
        raw, client = self.fresh()
        stub = Stubber(raw)
        stub.add_response("describe_ipams", {"Ipams": []})
        stub.activate()
        with self.assertRaisesRegex(ValueError, "no IPAM in us-east-1; pass the IPAM's home region"):
            aws_source.fetch(client)
        raw, client = self.fresh()
        stub = Stubber(raw)
        stub.add_response("describe_ipams", {"Ipams": [ipam()]}, {"IpamIds": ["ipam-0a1b2c3d4e5f60001"]})
        stub.add_response("describe_ipam_scopes", SCOPES)
        stub.activate()
        with self.assertRaisesRegex(ValueError, "has no scope 'ipam-scope-nope'"):
            aws_source.fetch(client, "ipam-0a1b2c3d4e5f60001", "ipam-scope-nope")

    def test_denied_names_the_permissions(self):
        stub = Stubber(self.raw)
        stub.add_client_error("describe_ipams", "UnauthorizedOperation")
        stub.activate()
        with self.assertRaisesRegex(RuntimeError, "ec2:GetIpamResourceCidrs .*AmazonEC2ReadOnlyAccess"):
            aws_source.fetch(self.client)

    def test_only_reads(self):
        with self.assertRaisesRegex(PermissionError, "read-only: refusing to call create_ipam"):
            self.client.create_ipam()
        with self.assertRaisesRegex(PermissionError, "refusing to paginate"):
            self.client.get_paginator("modify_ipam_pool")

    def test_cli(self):
        stub_ipam(self.raw)
        with tempfile.TemporaryDirectory() as d:
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(aws_source, "ec2_client", lambda *a: self.client), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                status = cli.main(["plan", "--from-aws", "-o", os.path.join(d, "import.csv"),
                                   "--save-export", os.path.join(d, "ipam.csv"),
                                   "--report", os.path.join(d, "report.md"),
                                   "--network-view", "aws-{account}",
                                   "--ea", "Owner=column:tag:Owner", "--ea", "Env=column:tag:Env"])
            self.assertEqual(status, 0, err.getvalue())
            with open(os.path.join(d, "import.csv"), encoding="utf-8") as handle:
                text = handle.read()
            with open(os.path.join(d, "report.md"), encoding="utf-8") as handle:
                report = handle.read()
            self.assertTrue(os.path.exists(os.path.join(d, "ipam.csv")))
        self.assertIn(f"network,10.15.0.0,255.255.0.0,aws-{A},on-prem DC reservation", text)
        self.assertIn(f"networkcontainer,10.0.0.0,16,aws-{A},prod (vpc-0a1b2c3d4e5f60001),,prod", text)
        if allocation_tags():
            self.assertIn(",netops,", text)
        self.assertIn("saved as", report)

    def test_input_choices(self):
        for argv, message in ((["plan", "-o", "x.csv"], "give an export file, or --from-aws"),
                              (["plan", "x.csv", "--from-aws", "-o", "x.csv"], "drop the export file"),
                              (["plan", "x.csv", "--ipam-id", "i", "-o", "x.csv"], "go with --from-aws")):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                self.assertEqual(cli.main(argv), 2)
            self.assertIn(message, err.getvalue())


class MissingBoto3Test(unittest.TestCase):
    def test_says_which_extra(self):
        with mock.patch.dict(sys.modules, {"boto3": None}):
            with self.assertRaisesRegex(RuntimeError, r"pip install 'ipam-infoblox\[aws\]'"):
                aws_source.ec2_client()


if __name__ == "__main__":
    unittest.main()
