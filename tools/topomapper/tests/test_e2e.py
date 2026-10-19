"""End to end through the CLI: AWS (mocked) + saved device output -> merge ->
filter -> every renderer, the way a user would chain the commands.

The AWS side has a site-to-site VPN whose customer gateway is edge1's
internet address in the device fixtures, so merge must stitch the two halves
together and the drawing must show that link.
"""

import io
import json
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from topomapper.cli import main  # noqa: E402
from topomapper.model import Topology  # noqa: E402

DEVICES = os.path.join(ROOT, "tests", "fixtures", "devices")
EDGE1_INTERNET = "203.0.113.2"  # edge1's ISP-facing address in configs/edge1.conf

FAKE_AWS = {
    "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SESSION_TOKEN": "testing", "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_CONFIG_FILE": os.devnull, "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
}

try:
    import boto3
    from moto import mock_aws
except ImportError:  # pragma: no cover
    boto3 = None


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        status = main(list(argv))
    return status, out.getvalue(), err.getvalue()


def build_aws():
    ec2 = boto3.client("ec2", region_name="us-east-1")
    vpc = ec2.create_vpc(CidrBlock="10.50.0.0/16")["Vpc"]["VpcId"]
    ec2.create_tags(Resources=[vpc], Tags=[{"Key": "Name", "Value": "prod"}])
    subnets = [ec2.create_subnet(VpcId=vpc, CidrBlock=f"10.50.{i}.0/24",
                                 AvailabilityZone=f"us-east-1{az}")["Subnet"]["SubnetId"]
               for i, az in ((1, "a"), (2, "b"))]
    igw = ec2.create_internet_gateway()["InternetGateway"]["InternetGatewayId"]
    ec2.attach_internet_gateway(InternetGatewayId=igw, VpcId=vpc)
    tgw = ec2.create_transit_gateway(Description="core")["TransitGateway"]["TransitGatewayId"]
    ec2.create_transit_gateway_vpc_attachment(TransitGatewayId=tgw, VpcId=vpc, SubnetIds=subnets)
    cgw = ec2.create_customer_gateway(Type="ipsec.1", PublicIp=EDGE1_INTERNET,
                                      BgpAsn=65010)["CustomerGateway"]["CustomerGatewayId"]
    ec2.create_vpn_connection(Type="ipsec.1", CustomerGatewayId=cgw, TransitGatewayId=tgw)
    return cgw


@unittest.skipIf(boto3 is None, "needs boto3 and moto")
class EndToEndTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def path(self, name):
        return os.path.join(self.tmp.name, name)

    def test_collect_merge_filter_render(self):
        with mock.patch.dict(os.environ, FAKE_AWS), mock_aws():
            cgw = build_aws()
            status, _, err = run("collect", "aws", "--region", "us-east-1", "-o", self.path("aws.json"))
        self.assertEqual(status, 0, err)

        status, _, err = run("collect", "files", DEVICES, "--site-map", os.path.join(DEVICES, "sites.csv"),
                             "--l3-exclude", "192.168.0.0/24", "-o", self.path("net.json"))
        self.assertEqual(status, 0, err)

        status, _, err = run("merge", self.path("aws.json"), self.path("net.json"),
                             "-o", self.path("all.json"))
        self.assertEqual(status, 0, err)
        merged = Topology.load(self.path("all.json"))
        self.assertEqual(merged.validate(), [])
        stitches = [l for l in merged.links.values() if l.kind == "stitch"]
        self.assertEqual(len(stitches), 1)
        self.assertEqual({stitches[0].source, stitches[0].target},
                         {"net:device:edge1", f"aws:cgw:{cgw}"})
        self.assertEqual(stitches[0].attrs["rule"], "cgw-ip")

        # Simplify for a diagram: drop AZ containers, keep everything else.
        status, _, err = run("filter", self.path("all.json"), "--exclude-kind", "az",
                             "-o", self.path("view.json"))
        self.assertEqual(status, 0, err)
        view = Topology.load(self.path("view.json"))
        self.assertFalse(view.by_kind("az"))
        self.assertEqual(view.validate(), [])

        outputs = {}
        for ext in ("drawio", "svg", "mmd", "dot"):
            out = self.path(f"view.{ext}")
            status, _, err = run("render", self.path("view.json"), "-o", out)
            self.assertEqual(status, 0, err)
            with open(out, encoding="utf-8") as f:
                outputs[ext] = f.read()

        drawio = ET.fromstring(outputs["drawio"])
        cells = {el.get("id") for el in drawio.iter() if el.get("id")}
        for node in ("net:device:edge1", f"aws:cgw:{cgw}", "net:device:core1"):
            self.assertIn(node, cells)
        self.assertTrue(any("stitch" in (el.get("id") or "") for el in drawio.iter()))
        ET.fromstring(outputs["svg"])
        self.assertIn("subgraph", outputs["mmd"])
        self.assertIn("cluster", outputs["dot"])

        # A second snapshot with one change: diff reports it with exit status 1.
        changed = json.loads(view.dumps())
        for node in changed["nodes"]:
            if node["id"] == "net:device:core1":
                node["attrs"]["os_version"] = "10.2(5)"
        with open(self.path("next.json"), "w", encoding="utf-8") as f:
            json.dump(changed, f)
        status, out, _ = run("diff", self.path("view.json"), self.path("next.json"))
        self.assertEqual(status, 1)
        self.assertIn("10.2(5)", out)
        status, _, _ = run("diff", self.path("view.json"), self.path("view.json"))
        self.assertEqual(status, 0)


if __name__ == "__main__":
    unittest.main()
