import argparse
import json
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from topomap.collectors import aws  # noqa: E402

try:
    import boto3
    from botocore.exceptions import ClientError
    from botocore.stub import Stubber
    from moto import mock_aws
except ImportError:  # pragma: no cover - optional dependencies
    boto3 = None

ACCOUNT = "123456789012"
OTHER = "111122223333"
REGION = "us-east-1"
FAKE_ENV = {
    "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SESSION_TOKEN": "testing", "AWS_DEFAULT_REGION": REGION,
    "AWS_CONFIG_FILE": os.devnull, "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
    "AWS_EC2_METADATA_DISABLED": "true",
}


def tag(resource_type, name):
    return [{"ResourceType": resource_type, "Tags": [{"Key": "Name", "Value": name}]}]


class Overlay:
    """A client that answers some operations itself and hands the rest to a
    real (moto) client. Answers are values or callables taking the call's
    keyword arguments."""

    def __init__(self, client, **answers):
        self._client, self._answers = client, answers

    def can_paginate(self, operation):
        return operation not in self._answers and self._client.can_paginate(operation)

    def __getattr__(self, name):
        if name in self._answers:
            answer = self._answers[name]
            return lambda **kw: answer(**kw) if callable(answer) else answer
        return getattr(self._client, name)


def denied(operation, code="AccessDenied"):
    def raise_error(**_):
        raise ClientError({"Error": {"Code": code, "Message": "denied"}}, operation)
    return raise_error


# moto doesn't implement these Direct Connect calls; answer them as empty.
NO_DX = {"describe_virtual_interfaces": {"virtualInterfaces": []},
         "describe_direct_connect_gateways": {"directConnectGateways": []}}


def parse(*argv):
    parser = argparse.ArgumentParser()
    aws.add_arguments(parser)
    return parser.parse_args(list(argv))


@unittest.skipIf(boto3 is None, "needs boto3 and moto: pip install 'topomap[test]'")
class AwsTestCase(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, FAKE_ENV)
        env.start()
        self.addCleanup(env.stop)
        for var in ("AWS_PROFILE", "AWS_DEFAULT_PROFILE"):
            os.environ.pop(var, None)
        moto = mock_aws()
        moto.start()
        self.addCleanup(moto.stop)
        self.overrides = {}   # (service, region or None) -> callable(real client) -> client
        real = aws._new_client

        def fake(session, service, region):
            client = real(session, service, region)
            for key in ((service, region), (service, None)):
                if key in self.overrides:
                    return self.overrides[key](client)
            if service == "directconnect":
                return Overlay(client, **NO_DX)
            return client
        patcher = mock.patch.object(aws, "_new_client", fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def collect(self, *argv):
        topo = aws.run(parse(*argv))
        self.assertEqual(topo.validate(), [])
        return topo

    def build_vpc(self, region=REGION, ec2=None):
        """A VPC with a public subnet (via the main route table), a public
        subnet with its own table, and a private subnet behind a NAT gateway."""
        ec2 = ec2 or boto3.client("ec2", region_name=region)
        vpc = ec2.create_vpc(CidrBlock="10.0.0.0/16", TagSpecifications=tag("vpc", "prod"))["Vpc"]
        vid = vpc["VpcId"]
        ec2.associate_vpc_cidr_block(VpcId=vid, CidrBlock="10.1.0.0/16")
        az_a, az_b = region + "a", region + "b"
        subnets = {}
        for name, cidr, az in (("web", "10.0.1.0/24", az_a), ("app", "10.0.2.0/24", az_b),
                               ("dmz", "10.0.3.0/24", az_a)):
            subnet = ec2.create_subnet(VpcId=vid, CidrBlock=cidr, AvailabilityZone=az,
                                       TagSpecifications=tag("subnet", name))["Subnet"]
            subnets[name] = subnet["SubnetId"]
        igw = ec2.create_internet_gateway()["InternetGateway"]["InternetGatewayId"]
        ec2.attach_internet_gateway(InternetGatewayId=igw, VpcId=vid)
        main = ec2.describe_route_tables(Filters=[{"Name": "vpc-id", "Values": [vid]},
                                                  {"Name": "association.main",
                                                   "Values": ["true"]}])["RouteTables"][0]
        ec2.create_route(RouteTableId=main["RouteTableId"], DestinationCidrBlock="0.0.0.0/0",
                         GatewayId=igw)
        dmz_table = ec2.create_route_table(VpcId=vid)["RouteTable"]["RouteTableId"]
        ec2.create_route(RouteTableId=dmz_table, DestinationCidrBlock="0.0.0.0/0", GatewayId=igw)
        ec2.associate_route_table(RouteTableId=dmz_table, SubnetId=subnets["dmz"])
        eip = ec2.allocate_address(Domain="vpc")["AllocationId"]
        nat = ec2.create_nat_gateway(SubnetId=subnets["web"], AllocationId=eip,
                                     ConnectivityType="public")["NatGateway"]
        private = ec2.create_route_table(VpcId=vid)["RouteTable"]["RouteTableId"]
        ec2.create_route(RouteTableId=private, DestinationCidrBlock="0.0.0.0/0",
                         NatGatewayId=nat["NatGatewayId"])
        ec2.associate_route_table(RouteTableId=private, SubnetId=subnets["app"])
        return {"vpc": vid, "igw": igw, "nat": nat["NatGatewayId"], **subnets}


class CoreTest(AwsTestCase):
    def test_vpc_subnets_and_containment(self):
        ids = self.build_vpc()
        topo = self.collect("--region", REGION)
        account = f"aws:account:{ACCOUNT}"
        region = f"aws:region:{ACCOUNT}:{REGION}"
        vpc = topo.nodes[f"aws:vpc:{ids['vpc']}"]
        self.assertEqual(topo.nodes[region].parent, account)
        self.assertEqual(vpc.parent, region)
        self.assertEqual(vpc.label, f"prod ({ids['vpc']})")
        self.assertEqual(vpc.attrs["cidrs"], ["10.0.0.0/16", "10.1.0.0/16"])
        self.assertEqual(vpc.attrs["account_id"], ACCOUNT)
        self.assertEqual(vpc.attrs["tags"], {"Name": "prod"})
        az = f"aws:az:{ids['vpc']}:{REGION}a"
        self.assertEqual(topo.nodes[az].parent, vpc.id)
        web = topo.nodes[f"aws:subnet:{ids['web']}"]
        self.assertEqual(web.parent, az)
        self.assertEqual(web.label, f"web ({ids['web']})")
        self.assertEqual(web.attrs["cidr"], "10.0.1.0/24")
        self.assertEqual(topo.nodes[f"aws:igw:{ids['igw']}"].parent, vpc.id)
        nat = topo.nodes[f"aws:nat:{ids['nat']}"]
        self.assertEqual(nat.parent, web.id)
        self.assertEqual(nat.attrs["connectivity"], "public")
        self.assertTrue(nat.attrs["public_ips"])
        self.assertEqual(topo.meta["collector"], "aws")
        self.assertEqual(topo.meta["accounts"], [ACCOUNT])
        self.assertEqual(topo.meta["regions"], [REGION])
        self.assertRegex(topo.meta["collected_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_public_and_private_subnets(self):
        ids = self.build_vpc()
        topo = self.collect("--region", REGION)
        subnet = lambda name: topo.nodes[f"aws:subnet:{ids[name]}"].attrs  # noqa: E731
        self.assertIs(subnet("web")["public"], True)    # no association: main table
        self.assertIs(subnet("dmz")["public"], True)    # explicit table
        self.assertIs(subnet("app")["public"], False)   # explicit table to a NAT gateway
        self.assertIn({"destination": "0.0.0.0/0", "target": ids["nat"]}, subnet("app")["routes"])
        self.assertIn({"destination": "10.0.0.0/16", "target": "local"}, subnet("web")["routes"])

    def test_include_extras(self):
        ids = self.build_vpc()
        ec2 = boto3.client("ec2", region_name=REGION)
        ec2.run_instances(ImageId="ami-12c6146b", MinCount=1, MaxCount=1, SubnetId=ids["app"],
                          InstanceType="t3.micro", TagSpecifications=tag("instance", "app1"))
        ec2.create_vpc_endpoint(VpcId=ids["vpc"], ServiceName=f"com.amazonaws.{REGION}.s3",
                                VpcEndpointType="Gateway")
        plain = self.collect("--region", REGION)
        self.assertEqual(plain.by_kind("instance", "vpc-endpoint"), [])
        full = self.collect("--region", REGION, "--include", "instances,endpoints")
        instance, = full.by_kind("instance")
        self.assertEqual(instance.parent, f"aws:subnet:{ids['app']}")
        self.assertTrue(instance.label.startswith("app1 (i-"))
        self.assertEqual(instance.attrs["instance_type"], "t3.micro")
        self.assertTrue(instance.attrs["private_ips"][0].startswith("10.0.2."))
        endpoint, = full.by_kind("vpc-endpoint")
        self.assertEqual(endpoint.parent, f"aws:vpc:{ids['vpc']}")
        self.assertEqual(endpoint.attrs["service"], f"com.amazonaws.{REGION}.s3")
        self.assertEqual(endpoint.attrs["endpoint_type"], "Gateway")

    def test_include_rejects_unknown(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            parse("--include", "instances,lambdas")

    def test_multi_region_and_deterministic(self):
        self.build_vpc(REGION)
        west = self.build_vpc("us-west-2")
        argv = ("--region", REGION, "--region", "us-west-2", "--workers", "4")
        topo = self.collect(*argv)
        self.assertEqual(topo.meta["regions"], [REGION, "us-west-2"])
        self.assertEqual(topo.nodes[f"aws:vpc:{west['vpc']}"].parent,
                         f"aws:region:{ACCOUNT}:us-west-2")
        self.assertEqual(len(topo.by_kind("vpc")), 2 + 2)  # plus each region's default VPC
        again = self.collect(*argv)
        again.meta["collected_at"] = topo.meta["collected_at"]
        self.assertEqual(again.dumps(), topo.dumps())

    def test_default_regions_come_from_describe_regions(self):
        regions = {"Regions": [{"RegionName": "us-west-2"}, {"RegionName": REGION}]}
        self.overrides[("ec2", None)] = lambda c: Overlay(c, describe_regions=regions)
        topo = self.collect()
        self.assertEqual(topo.meta["regions"], [REGION, "us-west-2"])
        self.assertIn(f"aws:region:{ACCOUNT}:us-west-2", topo.nodes)

    def test_region_listing_failure_falls_back(self):
        self.overrides[("ec2", None)] = lambda c: Overlay(
            c, describe_regions=denied("DescribeRegions", "UnauthorizedOperation"))
        topo = self.collect()
        self.assertEqual(topo.meta["regions"], [REGION])
        self.assertTrue(any("cannot list enabled regions" in w for w in topo.meta["warnings"]))


class PartialFailureTest(AwsTestCase):
    def test_denied_region_becomes_warning(self):
        ids = self.build_vpc()

        def deny_all(client):
            stub = Stubber(client)
            stub.add_client_error("describe_vpcs", "UnauthorizedOperation", http_status_code=403)
            stub.activate()
            return client
        self.overrides[("ec2", "us-west-2")] = deny_all
        topo = self.collect("--region", REGION, "--region", "us-west-2")
        self.assertIn(f"aws:vpc:{ids['vpc']}", topo.nodes)
        self.assertIn(f"aws:region:{ACCOUNT}:us-west-2", topo.nodes)
        self.assertEqual(topo.children(f"aws:region:{ACCOUNT}:us-west-2"), [])
        self.assertEqual(topo.meta["warnings"], [
            f"aws {ACCOUNT} us-west-2: cannot describe VPCs (UnauthorizedOperation); "
            "skipped EC2 in this region"])

    def test_one_denied_call_keeps_the_rest(self):
        ids = self.build_vpc()
        self.overrides[("ec2", REGION)] = lambda c: Overlay(
            c, describe_route_tables=denied("DescribeRouteTables"))
        topo = self.collect("--region", REGION)
        web = topo.nodes[f"aws:subnet:{ids['web']}"]
        self.assertNotIn("public", web.attrs)
        self.assertIn(f"aws:nat:{ids['nat']}", topo.nodes)
        self.assertIn(f"aws {ACCOUNT} {REGION}: cannot describe route tables (AccessDenied)",
                      topo.meta["warnings"])

    def test_no_credentials(self):
        for var in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_CREDENTIALS_FULL_URI",
                    "AWS_WEB_IDENTITY_TOKEN_FILE"):
            os.environ.pop(var, None)
        with self.assertRaisesRegex(RuntimeError, "no AWS credentials"):
            aws.run(parse("--region", REGION))

    def test_unknown_profile(self):
        with self.assertRaisesRegex(RuntimeError, "nope"):
            aws.run(parse("--profile", "nope", "--region", REGION))

    def test_missing_boto3(self):
        with mock.patch.dict(sys.modules, {"boto3": None}):
            with self.assertRaisesRegex(RuntimeError, r"pip install 'topomap\[aws\]'"):
                aws.run(parse())


class ReadOnlyTest(AwsTestCase):
    def test_proxy_refuses_writes(self):
        client = aws.ReadOnlyClient(boto3.client("ec2", region_name=REGION))
        self.assertIsInstance(client.describe_vpcs()["Vpcs"], list)
        self.assertTrue(client.can_paginate("describe_vpcs"))
        client.get_paginator("describe_vpcs")
        for name in ("create_vpc", "delete_vpc", "modify_vpc_attribute", "_make_api_call",
                     "generate_presigned_url"):
            with self.assertRaises(aws.ReadOnlyError, msg=name):
                getattr(client, name)
        with self.assertRaises(aws.ReadOnlyError):
            client.get_paginator("terminate_instances")
        self.assertEqual(boto3.client("ec2", region_name=REGION)
                         .describe_vpcs()["Vpcs"].__len__(), 1)  # nothing was created

    def test_assume_role_is_only_allowed_where_asked(self):
        sts = boto3.client("sts", region_name=REGION)
        with self.assertRaises(aws.ReadOnlyError):
            aws.ReadOnlyClient(sts).assume_role
        aws.ReadOnlyClient(sts, also=("assume_role",)).assume_role

    def test_collector_only_reads(self):
        self.build_vpc()
        used = []

        class Recorder:
            def __init__(self, client, service):
                self._client, self._service = client, service

            def get_paginator(self, operation):
                used.append((self._service, operation))
                return self._client.get_paginator(operation)

            def __getattr__(self, name):
                used.append((self._service, name))
                return getattr(self._client, name)
        real = aws._new_client
        with mock.patch.object(aws, "_new_client",
                               lambda s, svc, r: Recorder(Overlay(real(s, svc, r), **NO_DX)
                                                          if svc == "directconnect"
                                                          else real(s, svc, r), svc)):
            self.collect("--region", REGION, "--include", "instances,endpoints",
                         "--role-arn", f"arn:aws:iam::{OTHER}:role/topomap")
        calls = {name for _, name in used if name not in ("meta", "can_paginate", "get_paginator")}
        self.assertIn("describe_vpcs", calls)
        self.assertEqual({n for n in calls if not n.startswith(aws.READ_ONLY_PREFIXES)},
                         {"assume_role"})
        self.assertEqual({svc for svc, name in used if name == "assume_role"}, {"sts"})


class TransitAndVpnTest(AwsTestCase):
    def setUp(self):
        super().setUp()
        self.ec2 = boto3.client("ec2", region_name=REGION)
        self.ids = self.build_vpc(ec2=self.ec2)
        self.tgw = self.ec2.create_transit_gateway(
            TagSpecifications=tag("transit-gateway", "core"))["TransitGateway"]
        self.tgw_id = self.tgw["TransitGatewayId"]
        attachment = self.ec2.create_transit_gateway_vpc_attachment(
            TransitGatewayId=self.tgw_id, VpcId=self.ids["vpc"],
            SubnetIds=[self.ids["app"]])["TransitGatewayVpcAttachment"]
        self.attachment = attachment["TransitGatewayAttachmentId"]
        self.table = self.tgw["Options"]["AssociationDefaultRouteTableId"]
        self.ec2.create_transit_gateway_route(DestinationCidrBlock="10.0.0.0/16",
                                              TransitGatewayRouteTableId=self.table,
                                              TransitGatewayAttachmentId=self.attachment)

    def test_tgw_attachment_and_routes(self):
        topo = self.collect("--region", REGION)
        tgw = topo.nodes[f"aws:tgw:{self.tgw_id}"]
        self.assertEqual(tgw.parent, f"aws:region:{ACCOUNT}:{REGION}")
        self.assertEqual(tgw.label, f"core ({self.tgw_id})")
        self.assertEqual(tgw.attrs["asn"], 64512)
        tables = tgw.attrs["route_tables"]
        self.assertEqual([t["id"] for t in tables], [self.table])
        self.assertEqual(tables[0]["routes"][0]["destination"], "10.0.0.0/16")
        self.assertEqual(tables[0]["routes"][0]["type"], "static")
        link, = [l for l in topo.links.values() if l.kind == "tgw-attachment"]
        self.assertEqual({link.source, link.target}, {tgw.id, f"aws:vpc:{self.ids['vpc']}"})
        self.assertEqual(link.attrs["attachment_id"], self.attachment)
        self.assertEqual(link.attrs["resource_type"], "vpc")

    def test_tgw_routes_with_attachments(self):
        routes = {"Routes": [
            {"DestinationCidrBlock": "10.2.0.0/16", "Type": "propagated", "State": "active",
             "TransitGatewayAttachments": [{"TransitGatewayAttachmentId": "tgw-attach-1"},
                                           {"TransitGatewayAttachmentId": "tgw-attach-2"}]}]}
        self.overrides[("ec2", REGION)] = lambda c: Overlay(
            c, search_transit_gateway_routes=lambda **kw: dict(routes, kw=kw))
        topo = self.collect("--region", REGION)
        table, = topo.nodes[f"aws:tgw:{self.tgw_id}"].attrs["route_tables"]
        self.assertEqual(table["routes"], [
            {"destination": "10.2.0.0/16", "attachment": "tgw-attach-1", "type": "propagated"},
            {"destination": "10.2.0.0/16", "attachment": "tgw-attach-2", "type": "propagated"}])

    def test_tgw_route_search_filters_active(self):
        seen = []
        self.overrides[("ec2", REGION)] = lambda c: Overlay(
            c, search_transit_gateway_routes=lambda **kw: seen.append(kw) or {"Routes": []})
        self.collect("--region", REGION)
        self.assertEqual(seen[0]["Filters"], [{"Name": "state", "Values": ["active"]}])

    def test_vpn_to_tgw_and_vgw_with_tunnels(self):
        cgw = self.ec2.create_customer_gateway(Type="ipsec.1", PublicIp="203.0.113.10",
                                               BgpAsn=65010,
                                               TagSpecifications=tag("customer-gateway", "edge1")
                                               )["CustomerGateway"]["CustomerGatewayId"]
        to_tgw = self.ec2.create_vpn_connection(Type="ipsec.1", CustomerGatewayId=cgw,
                                                TransitGatewayId=self.tgw_id,
                                                Options={"StaticRoutesOnly": False}
                                                )["VpnConnection"]["VpnConnectionId"]
        vgw = self.ec2.create_vpn_gateway(Type="ipsec.1", AmazonSideAsn=64600)["VpnGateway"]
        vgw_id = vgw["VpnGatewayId"]
        self.ec2.attach_vpn_gateway(VpnGatewayId=vgw_id, VpcId=self.ids["vpc"])
        to_vgw = self.ec2.create_vpn_connection(Type="ipsec.1", CustomerGatewayId=cgw,
                                                VpnGatewayId=vgw_id,
                                                Options={"StaticRoutesOnly": True}
                                                )["VpnConnection"]["VpnConnectionId"]

        def with_tunnels(client):
            def describe(**kw):
                response = client.describe_vpn_connections(**kw)
                for i, vpn in enumerate(response["VpnConnections"]):
                    first, second = f"198.51.100.{i * 2 + 1}", f"198.51.100.{i * 2 + 2}"
                    options = vpn.setdefault("Options", {})
                    options["StaticRoutesOnly"] = vpn["VpnConnectionId"] == to_vgw
                    options["TunnelOptions"] = [
                        {"OutsideIpAddress": second, "TunnelInsideCidr": "169.254.10.4/30"},
                        {"OutsideIpAddress": first, "TunnelInsideCidr": "169.254.10.0/30"}]
                    vpn["VgwTelemetry"] = [{"OutsideIpAddress": first, "Status": "UP"},
                                           {"OutsideIpAddress": second, "Status": "DOWN"}]
                return response
            return Overlay(client, describe_vpn_connections=describe)
        self.overrides[("ec2", REGION)] = with_tunnels
        topo = self.collect("--region", REGION)

        cgw_node = topo.nodes[f"aws:cgw:{cgw}"]
        self.assertEqual(cgw_node.parent, f"aws:region:{ACCOUNT}:{REGION}")
        self.assertEqual(cgw_node.label, f"edge1 ({cgw})")
        self.assertEqual(cgw_node.attrs["ip"], "203.0.113.10")
        self.assertEqual(cgw_node.attrs["asn"], 65010)
        vgw_node = topo.nodes[f"aws:vgw:{vgw_id}"]
        self.assertEqual(vgw_node.parent, f"aws:vpc:{self.ids['vpc']}")
        self.assertEqual(vgw_node.attrs["asn"], 64600)
        self.assertTrue(any(l.kind == "vgw-attachment" for l in topo.links_of(vgw_node.id)))

        vpns = {l.attrs["vpn_id"]: l for l in topo.links.values() if l.kind == "vpn"}
        tgw_vpn, vgw_vpn = vpns[to_tgw], vpns[to_vgw]
        self.assertEqual({tgw_vpn.source, tgw_vpn.target}, {cgw_node.id, f"aws:tgw:{self.tgw_id}"})
        self.assertEqual({vgw_vpn.source, vgw_vpn.target}, {cgw_node.id, vgw_node.id})
        self.assertEqual(tgw_vpn.attrs["routing"], "bgp")
        self.assertEqual(vgw_vpn.attrs["routing"], "static")
        self.assertTrue(tgw_vpn.attrs["attachment_id"].startswith("tgw-attach-"))
        self.assertEqual(tgw_vpn.attrs["tunnels"][0],
                         {"outside_ip": tgw_vpn.attrs["tunnels"][0]["outside_ip"],
                          "inside_cidr": "169.254.10.0/30", "status": "UP"})
        self.assertEqual([t["status"] for t in tgw_vpn.attrs["tunnels"]], ["UP", "DOWN"])
        self.assertNotIn("CustomerGatewayConfiguration", json.dumps(topo.to_dict()))

    def test_peering_to_uncollected_account_gets_placeholder(self):
        sts = boto3.client("sts", region_name=REGION)
        creds = sts.assume_role(RoleArn=f"arn:aws:iam::{OTHER}:role/peer",
                                RoleSessionName="test")["Credentials"]
        other = boto3.client("ec2", region_name="us-west-2",
                             aws_access_key_id=creds["AccessKeyId"],
                             aws_secret_access_key=creds["SecretAccessKey"],
                             aws_session_token=creds["SessionToken"])
        peer_vpc = other.create_vpc(CidrBlock="172.16.0.0/16")["Vpc"]["VpcId"]
        peer_tgw = other.create_transit_gateway()["TransitGateway"]["TransitGatewayId"]
        pcx = self.ec2.create_vpc_peering_connection(
            VpcId=self.ids["vpc"], PeerVpcId=peer_vpc, PeerOwnerId=OTHER,
            PeerRegion="us-west-2")["VpcPeeringConnection"]["VpcPeeringConnectionId"]
        tgw_peering = self.ec2.create_transit_gateway_peering_attachment(
            TransitGatewayId=self.tgw_id, PeerTransitGatewayId=peer_tgw, PeerAccountId=OTHER,
            PeerRegion="us-west-2")["TransitGatewayPeeringAttachment"]["TransitGatewayAttachmentId"]

        topo = self.collect("--region", REGION)
        peer = topo.nodes[f"aws:vpc:{peer_vpc}"]
        self.assertIsNone(peer.parent)
        self.assertEqual(peer.label, peer_vpc)
        self.assertEqual(peer.attrs, {"external": True, "vpc_id": peer_vpc,
                                      "cidrs": ["172.16.0.0/16"], "account_id": OTHER,
                                      "region": "us-west-2"})
        link, = [l for l in topo.links.values() if l.kind == "vpc-peering"]
        self.assertEqual({link.source, link.target}, {peer.id, f"aws:vpc:{self.ids['vpc']}"})
        self.assertEqual(link.attrs["peering_id"], pcx)
        tgw_peer = topo.nodes[f"aws:tgw:{peer_tgw}"]
        self.assertTrue(tgw_peer.attrs["external"])
        self.assertEqual(tgw_peer.kind, "transit-gateway")
        link, = [l for l in topo.links.values() if l.kind == "tgw-peering"]
        self.assertEqual(link.attrs["attachment_id"], tgw_peering)
        self.assertEqual({link.source, link.target}, {tgw_peer.id, f"aws:tgw:{self.tgw_id}"})


DXGW = "0d3e3c6e-1111-4222-8333-444455556666"
DXCON = "dxcon-fgabc123"


class DirectConnectTest(AwsTestCase):
    def stub_dx(self, client):
        stub = Stubber(client)
        # Account-wide unit: gateways, then each one's associations and attachments.
        stub.add_response("describe_direct_connect_gateways", {"directConnectGateways": [
            {"directConnectGatewayId": DXGW, "directConnectGatewayName": "dx-core",
             "amazonSideAsn": 64700, "ownerAccount": ACCOUNT,
             "directConnectGatewayState": "available"}]})
        stub.add_response("describe_direct_connect_gateway_associations", {
            "directConnectGatewayAssociations": [
                {"directConnectGatewayId": DXGW, "associationId": "assoc-tgw",
                 "associationState": "associated",
                 "associatedGateway": {"id": "tgw-0aaa1111bbbb22223", "type": "transitGateway",
                                       "ownerAccount": ACCOUNT, "region": REGION},
                 "allowedPrefixesToDirectConnectGateway": [{"cidr": "10.0.0.0/8"}]},
                {"directConnectGatewayId": DXGW, "associationId": "assoc-vgw",
                 "associationState": "associated",
                 "associatedGateway": {"id": "vgw-0ccc3333dddd44445",
                                       "type": "virtualPrivateGateway",
                                       "ownerAccount": OTHER, "region": "us-west-2"}},
                {"directConnectGatewayId": DXGW, "associationId": "assoc-old",
                 "associationState": "disassociated",
                 "associatedGateway": {"id": "vgw-0eee", "type": "virtualPrivateGateway"}}]},
            {"directConnectGatewayId": DXGW})
        stub.add_response("describe_direct_connect_gateway_attachments", {
            "directConnectGatewayAttachments": [
                {"directConnectGatewayId": DXGW, "virtualInterfaceId": "dxvif-transit1",
                 "virtualInterfaceRegion": REGION, "virtualInterfaceOwnerAccount": ACCOUNT,
                 "attachmentState": "attached"}]},
            {"directConnectGatewayId": DXGW})
        # Region unit: connections, then virtual interfaces.
        stub.add_response("describe_connections", {"connections": [
            {"connectionId": DXCON, "connectionName": "dc1-primary", "connectionState": "available",
             "region": REGION, "location": "EqDC2", "bandwidth": "10Gbps", "ownerAccount": ACCOUNT,
             "tags": [{"key": "site", "value": "dc1"}]},
            {"connectionId": "dxcon-gone", "connectionState": "deleted"}]})
        vif = {"connectionId": DXCON, "ownerAccount": ACCOUNT, "region": REGION,
               "virtualInterfaceState": "available", "amazonSideAsn": 64700}
        stub.add_response("describe_virtual_interfaces", {"virtualInterfaces": [
            dict(vif, virtualInterfaceId="dxvif-transit1", virtualInterfaceName="transit-a",
                 virtualInterfaceType="transit", vlan=101, asn=65001,
                 customerAddress="192.0.2.2/30", amazonAddress="192.0.2.1/30",
                 directConnectGatewayId=DXGW),
            dict(vif, virtualInterfaceId="dxvif-private1", virtualInterfaceType="private",
                 vlan=102, asn=65001, virtualGatewayId="vgw-0fff5555aaaa66667"),
            dict(vif, virtualInterfaceId="dxvif-public1", virtualInterfaceType="public",
                 vlan=103, asn=65001, customerAddress="203.0.113.2/30"),
            dict(vif, connectionId="dxcon-hosted9", virtualInterfaceId="dxvif-hosted1",
                 virtualInterfaceType="private", vlan=104, asn=65002,
                 directConnectGatewayId=DXGW)]})
        stub.activate()
        self.addCleanup(stub.assert_no_pending_responses)
        return client

    def test_direct_connect(self):
        stubbed = []

        def one_stubbed_client(client):
            # The account-wide and regional units share one stub, in call order.
            if not stubbed:
                stubbed.append(self.stub_dx(client))
            return stubbed[0]
        self.overrides[("directconnect", None)] = one_stubbed_client
        topo = self.collect("--region", REGION)
        self.assertEqual(topo.meta.get("warnings"), None)

        gateway = topo.nodes[f"aws:dx-gateway:{DXGW}"]
        self.assertEqual(gateway.parent, f"aws:account:{ACCOUNT}")
        self.assertEqual(gateway.label, f"dx-core ({DXGW})")
        self.assertEqual(gateway.attrs["asn"], 64700)
        self.assertEqual(gateway.attrs["vif_attachments"], [
            {"vif_id": "dxvif-transit1", "region": REGION, "account_id": ACCOUNT,
             "state": "attached"}])

        conn = topo.nodes[f"aws:dx-connection:{DXCON}"]
        self.assertEqual(conn.parent, f"aws:region:{ACCOUNT}:{REGION}")
        self.assertEqual(conn.label, f"dc1-primary ({DXCON})")
        self.assertEqual((conn.attrs["location"], conn.attrs["bandwidth"], conn.attrs["state"]),
                         ("EqDC2", "10Gbps", "available"))
        self.assertEqual(conn.attrs["tags"], {"site": "dc1"})
        self.assertNotIn("aws:dx-connection:dxcon-gone", topo.nodes)

        vifs = {l.attrs["vif_id"]: l for l in topo.links.values() if l.kind == "dx-vif"}
        self.assertEqual(set(vifs), {"dxvif-transit1", "dxvif-private1", "dxvif-public1",
                                     "dxvif-hosted1"})
        transit = vifs["dxvif-transit1"]
        self.assertTrue(transit.directed)
        self.assertEqual((transit.source, transit.target), (conn.id, gateway.id))
        self.assertEqual(transit.label, "transit-a (dxvif-transit1)")
        self.assertEqual(transit.attrs["customer_address"], "192.0.2.2/30")
        self.assertEqual(transit.attrs["vlan"], 101)
        self.assertEqual(transit.attrs["vif_type"], "transit")
        self.assertEqual(transit.attrs["amazon_asn"], 64700)
        self.assertEqual(vifs["dxvif-private1"].target, "aws:vgw:vgw-0fff5555aaaa66667")
        self.assertEqual(vifs["dxvif-public1"].target, aws.PUBLIC_SERVICES)
        self.assertEqual(topo.nodes[aws.PUBLIC_SERVICES].kind, "external")
        hosted = topo.nodes["aws:dx-connection:dxcon-hosted9"]
        self.assertTrue(hosted.attrs["external"])

        assocs = {l.attrs["association_id"]: l for l in topo.links.values()
                  if l.kind == "dxgw-association"}
        self.assertEqual(set(assocs), {"assoc-tgw", "assoc-vgw"})
        self.assertEqual(assocs["assoc-tgw"].attrs["allowed_prefixes"], ["10.0.0.0/8"])
        self.assertIn("aws:tgw:tgw-0aaa1111bbbb22223",
                      (assocs["assoc-tgw"].source, assocs["assoc-tgw"].target))
        far_vgw = topo.nodes["aws:vgw:vgw-0ccc3333dddd44445"]
        self.assertEqual(far_vgw.attrs, {"external": True, "vgw_id": "vgw-0ccc3333dddd44445",
                                         "account_id": OTHER, "region": "us-west-2"})

    def test_denied_direct_connect_is_one_warning(self):
        self.overrides[("directconnect", None)] = lambda c: Overlay(
            c, describe_direct_connect_gateways=denied("DescribeDirectConnectGateways",
                                                       "AccessDeniedException"),
            describe_connections=denied("DescribeConnections", "AccessDeniedException"))
        topo = self.collect("--region", REGION)
        self.assertEqual(topo.meta["warnings"], [
            f"aws {ACCOUNT}: cannot describe Direct Connect gateways (AccessDeniedException); "
            "skipped Direct Connect gateways",
            f"aws {ACCOUNT} {REGION}: cannot describe Direct Connect connections "
            "(AccessDeniedException); skipped Direct Connect in this region"])


class AccountsTest(AwsTestCase):
    def other_vpc(self):
        sts = boto3.client("sts", region_name=REGION)
        creds = sts.assume_role(RoleArn=f"arn:aws:iam::{OTHER}:role/setup",
                                RoleSessionName="test")["Credentials"]
        session = boto3.Session(aws_access_key_id=creds["AccessKeyId"],
                                aws_secret_access_key=creds["SecretAccessKey"],
                                aws_session_token=creds["SessionToken"])
        session.client("iam", region_name=REGION).create_account_alias(AccountAlias="network")
        return session.client("ec2", region_name=REGION).create_vpc(
            CidrBlock="10.20.0.0/16")["Vpc"]["VpcId"]

    def test_role_arn_reaches_other_account(self):
        vpc = self.other_vpc()
        self.overrides[("sts", None)] = lambda c: Overlay(c, assume_role=lambda **kw: (
            denied("AssumeRole")() if "denied" in kw["RoleArn"] else c.assume_role(**kw)))
        topo = self.collect("--region", REGION,
                            "--role-arn", f"arn:aws:iam::{ACCOUNT}:role/denied",
                            "--role-arn", f"arn:aws:iam::{OTHER}:role/topomap")
        self.assertEqual(topo.meta["accounts"], [OTHER])
        account = topo.nodes[f"aws:account:{OTHER}"]
        self.assertEqual(account.label, f"network ({OTHER})")
        self.assertEqual(account.attrs["name"], "network")
        self.assertNotIn(f"aws:account:{ACCOUNT}", topo.nodes)
        self.assertEqual(topo.nodes[f"aws:vpc:{vpc}"].parent, f"aws:region:{OTHER}:{REGION}")
        self.assertIn(f"aws: cannot assume arn:aws:iam::{ACCOUNT}:role/denied (AccessDenied)",
                      topo.meta["warnings"])

    def test_org_role_name(self):
        vpc = self.other_vpc()
        accounts = {"Accounts": [
            {"Id": ACCOUNT, "Name": "management", "Status": "ACTIVE"},
            {"Id": OTHER, "Name": "network", "Status": "ACTIVE"}]}
        assumed = []
        self.overrides[("organizations", None)] = lambda c: Overlay(c, list_accounts=accounts)
        self.overrides[("sts", None)] = lambda c: Overlay(
            c, assume_role=lambda **kw: assumed.append(kw) or c.assume_role(**kw))
        topo = self.collect("--region", REGION, "--org-role-name", "OrgRead",
                            "--external-id", "ext-1")
        self.assertEqual(topo.meta["accounts"], [OTHER, ACCOUNT])
        self.assertEqual(assumed, [{"RoleArn": f"arn:aws:iam::{OTHER}:role/OrgRead",
                                    "RoleSessionName": "topomap", "ExternalId": "ext-1"}])
        self.assertEqual(topo.nodes[f"aws:account:{ACCOUNT}"].label, f"management ({ACCOUNT})")
        self.assertIn(f"aws:vpc:{vpc}", topo.nodes)

    def test_nothing_reachable_is_an_error(self):
        self.overrides[("sts", None)] = lambda c: Overlay(c, assume_role=denied("AssumeRole"))
        with self.assertRaisesRegex(RuntimeError, "no AWS account could be reached"):
            aws.run(parse("--region", REGION, "--role-arn", f"arn:aws:iam::{OTHER}:role/x"))

    def test_bad_role_arn_is_rejected(self):
        with mock.patch("sys.stderr"), self.assertRaises(SystemExit):
            parse("--role-arn", "arn:aws:iam::12345:user/bob")

    def test_assumed_role_is_renewed_before_expiry(self):
        renewals = []
        session = boto3.Session()

        def renew():
            renewals.append(1)
            return session, aws._now() + timedelta(hours=1)
        account = aws._Account(OTHER, session, REGION, renew=renew,
                               expires=aws._now() + timedelta(minutes=1))
        account.client("ec2")
        account.client("ec2")
        self.assertEqual(renewals, [1])


class CliTest(AwsTestCase):
    def test_cli_writes_topology(self):
        from topomap import cli
        from topomap.model import Topology
        ids = self.build_vpc()
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "aws.json")
            self.assertEqual(cli.main(["collect", "aws", "--region", REGION, "-o", path]), 0)
            topo = Topology.load(path)
        self.assertEqual(topo.validate(), [])
        self.assertEqual(topo.meta["collector"], "aws")
        self.assertIn(f"aws:vpc:{ids['vpc']}", topo.nodes)
        self.assertIn(f"aws:subnet:{ids['web']}", topo.nodes)


if __name__ == "__main__":
    unittest.main()
