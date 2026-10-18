"""--all-resources. moto runs the VPCs that resources sit in, but its Config
`SelectResourceConfig` returns nothing and it has no Resource Explorer or
Cloud Control, so those answers come from Overlays."""

import json
import os
import sys
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from test_aws import ACCOUNT, REGION, AwsTestCase, Overlay, boto3, denied  # noqa: E402
from topomap.collectors import aws, aws_inventory  # noqa: E402
from topomap.render import render  # noqa: E402

ROLE_ID = "AROAEXAMPLEROLE0001"
ROLE_ARN = f"arn:aws:iam::{ACCOUNT}:role/app-runtime"
FUNCTION = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:orders-api"
QUEUE = f"arn:aws:sqs:{REGION}:{ACCOUNT}:orders"
VIEW = f"arn:aws:resource-explorer-2:{REGION}:{ACCOUNT}:view/default/1"


def ci(arn, rtype, rid, name=None, region=REGION, rels=(), tags=None, status="OK"):
    """One AWS Config configuration item, as SelectResourceConfig returns it."""
    return json.dumps({
        "arn": arn, "resourceType": rtype, "resourceId": rid, "resourceName": name,
        "awsRegion": region, "configurationItemStatus": status,
        "relationships": [{"relationshipName": r, "resourceType": t, "resourceId": i}
                          for r, t, i in rels],
        "tags": [{"key": k, "value": v} for k, v in (tags or {}).items()]})


def config(items, recording=True):
    pages = [items[i:i + 2] for i in range(0, len(items), 2)] or [[]]

    def select(Expression, NextToken=None):
        page = int(NextToken or 0)
        answer = {"Results": pages[page]}
        if page + 1 < len(pages):
            answer["NextToken"] = str(page + 1)
        return answer
    return lambda c: Overlay(c, describe_configuration_recorder_status={
        "ConfigurationRecordersStatus": [{"name": "default", "recording": recording}]},
        select_resource_config=select)


def explorer(resources):
    return lambda c: Overlay(c, get_index={"State": "ACTIVE", "Type": "LOCAL"},
                             get_default_view={"ViewArn": VIEW},
                             list_resources=lambda **kw: {"Resources": resources})


def found(arn, rtype, region=REGION, tags=None):
    props = [{"Name": "tags", "Data": [{"Key": k, "Value": v} for k, v in tags.items()]}] \
        if tags else []
    return {"Arn": arn, "ResourceType": rtype, "Region": region, "Service": arn.split(":")[2],
            "OwningAccountId": ACCOUNT, "Properties": props}


class InventoryTestCase(AwsTestCase):
    def setUp(self):
        super().setUp()
        self.overrides[("resource-explorer-2", None)] = lambda c: Overlay(
            c, get_index=denied("GetIndex", "ResourceNotFoundException"))
        self.listed = {}   # Cloud Control type -> answer (a list, or a callable that raises)
        self.overrides[("cloudcontrol", None)] = lambda c: Overlay(c, list_resources=self._cc)
        self.overrides[("resourcegroupstaggingapi", None)] = lambda c: Overlay(
            c, get_resources={"ResourceTagMappingList": []})

    def _cc(self, TypeName, NextToken=None):
        answer = self.listed.get(TypeName, [])
        if callable(answer):
            return answer()
        return {"ResourceDescriptions": answer}

    def inventory(self, *extra):
        return self.collect("--region", REGION, "--all-resources", *extra)


class ConfigEstate:
    def build(self):
        ids = self.build_vpc()
        sg = boto3.client("ec2", region_name=REGION).create_security_group(
            GroupName="web", Description="web", VpcId=ids["vpc"])["GroupId"]
        items = [
            ci(f"arn:aws:ec2:{REGION}:{ACCOUNT}:vpc/{ids['vpc']}", "AWS::EC2::VPC", ids["vpc"]),
            ci(FUNCTION, "AWS::Lambda::Function", "orders-api", "orders-api", rels=[
                ("Is associated with Subnet", "AWS::EC2::Subnet", ids["web"]),
                ("Is associated with Subnet", "AWS::EC2::Subnet", ids["app"]),
                ("Is associated with SecurityGroup", "AWS::EC2::SecurityGroup", sg),
                ("Is associated with Role", "AWS::IAM::Role", ROLE_ID)]),
            ci(f"arn:aws:ec2:{REGION}:{ACCOUNT}:security-group/{sg}", "AWS::EC2::SecurityGroup",
               sg, "web", rels=[("Is contained in Vpc", "AWS::EC2::VPC", ids["vpc"])]),
            ci(f"arn:aws:ec2:{REGION}:{ACCOUNT}:network-interface/eni-0abc", "AWS::EC2::NetworkInterface",
               "eni-0abc", rels=[("Is contained in Subnet", "AWS::EC2::Subnet", ids["web"])]),
            ci("arn:aws:s3:::acme-data", "AWS::S3::Bucket", "acme-data", "acme-data",
               tags={"team": "data"}),
            ci(ROLE_ARN, "AWS::IAM::Role", ROLE_ID, "app-runtime", region="global"),
            ci(QUEUE, "AWS::SQS::Queue", QUEUE, "orders", status="ResourceDeleted"),
            ci(f"arn:aws:config:{REGION}:{ACCOUNT}:resource-compliance/x",
               "AWS::Config::ResourceCompliance", "x"),
        ]
        self.overrides[("config", None)] = config(items)
        return ids, sg


class ConfigTest(ConfigEstate, InventoryTestCase):
    def test_resources_sit_where_config_says(self):
        ids, sg = self.build()
        topo = self.inventory()
        nodes = topo.nodes
        fn = nodes[f"aws:resource:{FUNCTION}"]
        self.assertEqual(fn.kind, "aws-resource")
        self.assertEqual(fn.label, "orders-api")
        self.assertEqual(fn.parent, f"aws:vpc:{ids['vpc']}")  # two subnets: the VPC
        self.assertEqual(fn.attrs["resource_type"], "AWS::Lambda::Function")
        self.assertEqual(fn.attrs["service"], "lambda")
        self.assertEqual(fn.attrs["found_by"], ["config"])
        group = nodes[f"aws:resource:arn:aws:ec2:{REGION}:{ACCOUNT}:security-group/{sg}"]
        self.assertEqual(group.parent, f"aws:vpc:{ids['vpc']}")
        eni = nodes[f"aws:resource:arn:aws:ec2:{REGION}:{ACCOUNT}:network-interface/eni-0abc"]
        self.assertEqual(eni.parent, f"aws:subnet:{ids['web']}")
        bucket = nodes["aws:resource:arn:aws:s3:::acme-data"]
        self.assertEqual(bucket.parent, f"aws:service:{ACCOUNT}:{REGION}:s3")
        self.assertEqual(nodes[bucket.parent].label, "S3")
        self.assertEqual(nodes[bucket.parent].attrs["resources"], 1)
        self.assertNotIn("tags", bucket.attrs)  # tags only with --include tags
        role = nodes[f"aws:resource:{ROLE_ARN}"]
        self.assertEqual(role.parent, f"aws:service:{ACCOUNT}:global:iam")
        self.assertEqual(nodes[role.parent].parent, f"aws:account:{ACCOUNT}")

    def test_network_resources_keep_their_nodes(self):
        ids, _ = self.build()
        plain = self.collect("--region", REGION)
        topo = self.inventory()
        self.assertEqual(sorted(n.id for n in topo.by_kind("vpc")),
                         sorted(n.id for n in plain.by_kind("vpc")))
        self.assertNotIn("found_by", topo.nodes[f"aws:vpc:{ids['vpc']}"].attrs)
        # Every service box that placement emptied is gone.
        for group in topo.by_kind("aws-service"):
            self.assertTrue(topo.children(group.id), group.id)
        self.assertNotIn(f"aws:service:{ACCOUNT}:{REGION}:lambda", topo.nodes)

    def test_deleted_and_report_items_are_left_out(self):
        self.build()
        topo = self.inventory()
        self.assertNotIn(f"aws:resource:{QUEUE}", topo.nodes)
        self.assertFalse([n for n in topo.nodes if "resource-compliance" in n])
        self.assertEqual(topo.nodes[f"aws:region:{ACCOUNT}:{REGION}"].attrs["inventory_sources"],
                         ["config"])
        self.assertNotIn("warnings", topo.meta)

    def test_relationships_on_request(self):
        self.build()
        self.assertFalse([l for l in self.inventory().links.values() if l.kind == "related"])
        topo = self.inventory("--include", "relationships")
        related = sorted((l.source.split(":")[-1], l.attrs["relation"], l.target.split("/")[-1])
                         for l in topo.links.values() if l.kind == "related")
        # The subnets placed the function, so they aren't drawn as links too.
        self.assertIn(("orders-api", "Is associated with Role", "app-runtime"), related)
        self.assertEqual(len([r for r in related if r[0] == "orders-api"]), 2)  # role and SG

    def test_tags_and_skip_network(self):
        ids, sg = self.build()
        topo = self.inventory("--include", "tags", "--skip-network")
        self.assertEqual(topo.nodes["aws:resource:arn:aws:s3:::acme-data"].attrs["tags"],
                         {"team": "data"})
        # Without the network collection the VPC is the inventory's, with its own kind.
        vpc = topo.nodes[f"aws:vpc:{ids['vpc']}"]
        self.assertEqual(vpc.kind, "vpc")
        self.assertEqual(vpc.parent, f"aws:region:{ACCOUNT}:{REGION}")
        # Config names only the function's subnets, which nothing collected here.
        self.assertEqual(topo.nodes[f"aws:resource:{FUNCTION}"].parent,
                         f"aws:service:{ACCOUNT}:{REGION}:lambda")
        self.assertEqual(topo.nodes[f"aws:resource:arn:aws:ec2:{REGION}:{ACCOUNT}:security-group/{sg}"]
                         .parent, vpc.id)

    def test_output_is_deterministic(self):
        self.build()
        one, two = self.inventory(), self.inventory()
        one.meta.pop("collected_at"), two.meta.pop("collected_at")
        self.assertEqual(one.dumps(), two.dumps())


class SkipNetworkStructureTest(InventoryTestCase):
    def test_vpc_subnet_and_instance_from_config(self):
        vpc, subnet, instance = "vpc-0aaa", "subnet-0bbb", "i-0ccc"
        self.overrides[("config", None)] = config([
            ci(f"arn:aws:ec2:{REGION}:{ACCOUNT}:vpc/{vpc}", "AWS::EC2::VPC", vpc,
               tags={"Name": "prod"}),
            ci(f"arn:aws:ec2:{REGION}:{ACCOUNT}:subnet/{subnet}", "AWS::EC2::Subnet", subnet,
               rels=[("Is contained in Vpc", "AWS::EC2::VPC", vpc)]),
            ci(f"arn:aws:ec2:{REGION}:{ACCOUNT}:instance/{instance}", "AWS::EC2::Instance",
               instance, rels=[("Is contained in Subnet", "AWS::EC2::Subnet", subnet)]),
        ])
        topo = self.inventory("--skip-network")
        self.assertEqual(topo.nodes[f"aws:vpc:{vpc}"].label, f"prod ({vpc})")
        self.assertEqual(topo.nodes[f"aws:subnet:{subnet}"].parent, f"aws:vpc:{vpc}")
        self.assertEqual(topo.nodes[f"aws:instance:{instance}"].kind, "instance")
        self.assertEqual(topo.nodes[f"aws:instance:{instance}"].parent, f"aws:subnet:{subnet}")
        self.assertFalse(topo.by_kind("aws-service"))


class ResourceExplorerTest(InventoryTestCase):
    def test_untagged_resources_and_merging_with_config(self):
        self.build_vpc()
        self.overrides[("config", None)] = config([
            ci(FUNCTION, "AWS::Lambda::Function", "orders-api", "orders-api")])
        self.overrides[("resource-explorer-2", None)] = explorer([
            found(FUNCTION, "lambda:function", tags={"Name": "Orders API"}),
            found(QUEUE, "sqs:queue"),
            found(f"arn:aws:cloudfront::{ACCOUNT}:distribution/E2EXAMPLE", "cloudfront:distribution",
                  region="global"),
        ])
        topo = self.inventory()
        fn = topo.nodes[f"aws:resource:{FUNCTION}"]
        self.assertEqual(fn.attrs["found_by"], ["config", "resource-explorer"])
        self.assertEqual(fn.attrs["resource_type"], "AWS::Lambda::Function")
        queue = topo.nodes[f"aws:resource:{QUEUE}"]
        self.assertEqual((queue.label, queue.attrs["resource_type"]), ("orders", "sqs:queue"))
        cdn = topo.nodes[f"aws:resource:arn:aws:cloudfront::{ACCOUNT}:distribution/E2EXAMPLE"]
        self.assertEqual(cdn.parent, f"aws:service:{ACCOUNT}:global:cloudfront")
        self.assertEqual(topo.nodes[f"aws:region:{ACCOUNT}:{REGION}"].attrs["inventory_sources"],
                         ["config", "resource-explorer"])

    def test_no_default_view_is_explained(self):
        self.overrides[("resource-explorer-2", None)] = lambda c: Overlay(
            c, get_index={"State": "ACTIVE"},
            get_default_view=denied("GetDefaultView", "ResourceNotFoundException"))
        topo = self.inventory("--skip-network")
        self.assertTrue(any("no default view" in w for w in topo.meta["warnings"]))


class FallbackTest(InventoryTestCase):
    def test_cloud_control_and_tagging_with_one_warning(self):
        ids = self.build_vpc()
        self.listed = {
            "AWS::Lambda::Function": [{"Identifier": "resize", "Properties": json.dumps(
                {"VpcConfig": {"SubnetIds": [ids["web"]]}})}],
            "AWS::SQS::Queue": [{"Identifier": f"https://sqs.{REGION}.amazonaws.com/{ACCOUNT}/jobs",
                                 "Properties": "{}"}],
            "AWS::S3::Bucket": [{"Identifier": "acme-logs", "Properties": "{}"}],
            "AWS::AutoScaling::AutoScalingGroup": [{"Identifier": "web-asg", "Properties": "{}"}],
            "AWS::MSK::Cluster": lambda: denied("ListResources", "UnsupportedActionException")(),
            "AWS::KMS::Key": lambda: denied("ListResources", "AccessDeniedException")(),
        }
        self.overrides[("resourcegroupstaggingapi", None)] = lambda c: Overlay(
            c, get_resources={"ResourceTagMappingList": [
                {"ResourceARN": f"arn:aws:sns:{REGION}:{ACCOUNT}:alerts",
                 "Tags": [{"Key": "Name", "Value": "alerts"}]},
                {"ResourceARN": f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:resize", "Tags": []}]})
        topo = self.inventory()
        fn = topo.nodes[f"aws:resource:arn:aws:lambda:{REGION}:{ACCOUNT}:function:resize"]
        self.assertEqual(fn.parent, f"aws:subnet:{ids['web']}")
        self.assertEqual(fn.attrs["found_by"], ["cloud-control", "tagging"])
        self.assertIn(f"aws:resource:arn:aws:sqs:{REGION}:{ACCOUNT}:jobs", topo.nodes)
        self.assertEqual(topo.nodes["aws:resource:arn:aws:s3:::acme-logs"].parent,
                         f"aws:service:{ACCOUNT}:global:s3")
        asg = topo.nodes[f"aws:resource:{ACCOUNT}:{REGION}:AWS::AutoScaling::AutoScalingGroup:web-asg"]
        self.assertIsNone(asg.attrs.get("arn"))
        self.assertEqual(asg.parent, f"aws:service:{ACCOUNT}:{REGION}:autoscaling")
        self.assertIn(f"aws:resource:arn:aws:sns:{REGION}:{ACCOUNT}:alerts", topo.nodes)
        warnings = topo.meta["warnings"]
        self.assertEqual(sum("neither AWS Config nor Resource Explorer" in w for w in warnings), 1)
        self.assertEqual(sum("could not list 1 resource type" in w for w in warnings), 1)

    def test_global_types_only_in_the_home_region(self):
        asked = []
        self.overrides[("cloudcontrol", None)] = lambda c: Overlay(
            c, list_resources=lambda TypeName, **kw: asked.append((c.meta.region_name, TypeName))
            or {"ResourceDescriptions": []})
        self.collect("--region", REGION, "--region", "eu-west-1", "--all-resources",
                     "--skip-network")
        iam = [r for r, t in asked if t == "AWS::IAM::Role"]
        self.assertEqual(iam, [REGION])
        self.assertEqual(len([r for r, t in asked if t == "AWS::Lambda::Function"]), 2)

    def test_one_failing_source_keeps_the_others(self):
        self.listed = {"AWS::SNS::Topic": [{"Identifier": f"arn:aws:sns:{REGION}:{ACCOUNT}:alerts"}]}
        self.overrides[("resourcegroupstaggingapi", None)] = lambda c: Overlay(
            c, get_resources=lambda **kw: {}["unexpected shape"])
        topo = self.inventory("--skip-network")
        self.assertIn(f"aws:resource:arn:aws:sns:{REGION}:{ACCOUNT}:alerts", topo.nodes)
        self.assertTrue(any("tagging: skipped after unexpected KeyError" in w
                            for w in topo.meta["warnings"]))

    def test_denied_config_falls_back(self):
        self.overrides[("config", None)] = lambda c: Overlay(
            c, describe_configuration_recorder_status=denied("DescribeConfigurationRecorderStatus"))
        topo = self.inventory("--skip-network")
        self.assertTrue(any("cannot read config (AccessDenied)" in w for w in topo.meta["warnings"]))
        self.assertEqual(topo.nodes[f"aws:region:{ACCOUNT}:{REGION}"].attrs["inventory_sources"],
                         ["cloud-control", "tagging"])


class DedupeTest(InventoryTestCase):
    def test_iam_and_inventory_share_a_role(self):
        iam = boto3.client("iam", region_name=REGION)
        arn = iam.create_role(RoleName="app-runtime", AssumeRolePolicyDocument=json.dumps(
            {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "sts:AssumeRole",
                                                     "Principal": {"Service": "lambda.amazonaws.com"}}]})
        )["Role"]["Arn"]
        self.overrides[("config", None)] = config([
            ci(arn, "AWS::IAM::Role", ROLE_ID, "app-runtime", region="global")])
        topo = self.inventory("--iam", "--skip-network")
        self.assertNotIn(f"aws:resource:{arn}", topo.nodes)
        self.assertEqual(len([n for n in topo.nodes.values() if n.attrs.get("arn") == arn]), 1)
        self.assertNotIn(f"aws:service:{ACCOUNT}:global:iam", topo.nodes)


class RenderTest(ConfigEstate, InventoryTestCase):
    def test_formats(self):
        self.build()
        topo = self.inventory("--include", "relationships")
        drawio = render(topo, "drawio")
        ET.fromstring(drawio)
        self.assertIn("resIcon=mxgraph.aws4.lambda", drawio)
        self.assertIn("resIcon=mxgraph.aws4.s3", drawio)
        svg = render(topo, "svg")
        ET.fromstring(svg)
        self.assertIn("orders-api", svg)
        for fmt in ("mermaid", "dot"):
            self.assertIn("orders-api", render(topo, fmt))


class ReadOnlyTest(unittest.TestCase):
    def test_select_needs_an_explicit_allowance(self):
        class Fake:
            def select_resource_config(self, **kw):
                return {"Results": []}
        with self.assertRaises(aws.ReadOnlyError):
            aws.ReadOnlyClient(Fake()).select_resource_config(Expression="SELECT 1")
        allowed = aws.ReadOnlyClient(Fake(), also=("select_resource_config",))
        self.assertEqual(allowed.select_resource_config(Expression="SELECT 1"), {"Results": []})

    def test_arns(self):
        self.assertEqual(aws_inventory.parse_arn("arn:aws:s3:::acme"),
                         ("s3", "", "", "bucket", "acme"))
        self.assertEqual(aws_inventory.parse_arn(ROLE_ARN)[3:], ("role", "app-runtime"))
        self.assertIsNone(aws_inventory.parse_arn("not-an-arn"))


if __name__ == "__main__":
    unittest.main()
