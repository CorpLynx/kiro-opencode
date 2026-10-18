import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gcp_fake  # noqa: E402
from gcp_fake import (APP_DEV, APP_PROD, COMPUTE, HOST, IC_CUSTOMER_IP, KEYS, ONPREM_VPN_IP,  # noqa: E402
                      ORG, SHARED)
from topomap.cli import main  # noqa: E402
from topomap.collectors import gcp, gcp_org  # noqa: E402
from topomap.merge import merge  # noqa: E402
from topomap.model import Topology  # noqa: E402
from topomap.render import render  # noqa: E402

ALL = sorted(gcp_fake.NUMBERS)
NET = f"gcp:network:{HOST}:shared-vpc"
USE4 = f"gcp:region:{HOST}:shared-vpc:us-east4"
ROUTER = f"gcp:router:{HOST}:us-east4:cr-use4"
HA_VPN = f"gcp:vpn-gateway:{HOST}:us-east4:ha-vpn-use4"
ONPREM = f"gcp:external-vpn-gateway:{HOST}:onprem-edge"
DEDICATED = f"gcp:interconnect:{HOST}:ic-dedicated-1"
PARTNER = f"gcp:interconnect:{HOST}:partner-att-partner"
HUB = f"gcp:ncc-hub:{HOST}:global-hub"


def collect(overrides=None, projects=ALL, **kwargs):
    api, fake = gcp_fake.api(overrides)
    return gcp.collect(api, projects, **kwargs), fake


def links(topo, kind):
    return sorted((l for l in topo.links.values() if l.kind == kind), key=lambda l: l.id)


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        status = main(list(argv))
    return status, out.getvalue(), err.getvalue()


class NetworkTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.topo, cls.fake = collect()

    def test_valid_and_deterministic(self):
        self.assertEqual(self.topo.validate(), [])
        again, _ = collect(workers=1)
        self.assertEqual(self.topo.dumps().replace(self.topo.meta["collected_at"], ""),
                         again.dumps().replace(again.meta["collected_at"], ""))

    def test_containment(self):
        nodes = self.topo.nodes
        self.assertEqual(nodes[NET].parent, f"gcp:project:{HOST}")
        self.assertEqual(nodes[NET].attrs["routing_mode"], "GLOBAL")
        self.assertEqual(nodes[USE4].parent, NET)
        subnet = nodes[f"gcp:subnet:{HOST}:us-east4:prod-use4"]
        self.assertEqual(subnet.parent, USE4)
        self.assertEqual(subnet.attrs["cidr"], "10.10.0.0/20")
        self.assertEqual(subnet.attrs["secondary_ranges"], [{"name": "pods", "cidr": "10.64.0.0/14"}])
        # The second page of the aggregated list.
        self.assertEqual(nodes[f"gcp:subnet:{HOST}:us-central1:prod-usc1"].parent,
                         f"gcp:region:{HOST}:shared-vpc:us-central1")
        self.assertEqual(nodes[ROUTER].parent, USE4)
        self.assertEqual(nodes[ROUTER].attrs["asn"], 64512)
        self.assertEqual(nodes[ROUTER].attrs["nats"], ["nat-use4"])
        self.assertEqual(nodes[HA_VPN].parent, USE4)
        self.assertEqual(nodes[ONPREM].parent, f"gcp:project:{HOST}")
        self.assertEqual(nodes[HUB].parent, f"gcp:project:{HOST}")

    def test_vpn_tunnels(self):
        (vpn,) = links(self.topo, "vpn")
        self.assertEqual({vpn.source, vpn.target}, {HA_VPN, ONPREM})
        self.assertEqual([t["name"] for t in vpn.attrs["tunnels"]], ["tun-0", "tun-1"])
        self.assertEqual({t["status"] for t in vpn.attrs["tunnels"]}, {"ESTABLISHED"})
        self.assertNotIn("never-read", self.topo.dumps())  # secret hashes, metadata

    def test_interconnect_attachments(self):
        attachments = {l.attrs["attachment"]: l for l in links(self.topo, "interconnect-attachment")}
        dedicated = attachments["att-1"]
        self.assertEqual({dedicated.source, dedicated.target}, {ROUTER, DEDICATED})
        self.assertEqual(dedicated.attrs["vlan"], 100)
        self.assertEqual(dedicated.attrs["customer_address"], f"{IC_CUSTOMER_IP}/29")
        partner = attachments["att-partner"]
        self.assertEqual({partner.source, partner.target}, {ROUTER, PARTNER})
        self.assertEqual(self.topo.nodes[PARTNER].label, "Example Carrier")
        self.assertEqual(self.topo.nodes[PARTNER].attrs["type"], "PARTNER")
        self.assertEqual(self.topo.nodes[DEDICATED].attrs["link_type"], "LINK_TYPE_ETHERNET_10G_LR")

    def test_shared_vpc(self):
        (link,) = links(self.topo, "shared-vpc")
        self.assertEqual((link.source, link.target), (f"gcp:project:{APP_PROD}", f"gcp:project:{HOST}"))
        self.assertTrue(link.directed)
        self.assertEqual(self.topo.nodes[f"gcp:project:{HOST}"].attrs["shared_vpc"], "host")
        self.assertEqual(self.topo.nodes[f"gcp:project:{APP_PROD}"].attrs["shared_vpc"], "service")

    def test_ncc_spoke_to_the_tunnel_gateway(self):
        (spoke,) = links(self.topo, "ncc-spoke")
        self.assertEqual((spoke.source, spoke.target), (HUB, HA_VPN))
        self.assertEqual(spoke.attrs["spoke"], "vpn-spoke")

    def test_peered_network_is_a_placeholder(self):
        peer = self.topo.nodes["gcp:network:partner-proj:partner-net"]
        self.assertTrue(peer.attrs["external"])
        (peering,) = links(self.topo, "vpc-peering")
        self.assertEqual(peering.label, "to-partner")

    def test_compute_disabled_projects(self):
        self.assertEqual(self.topo.meta["compute_disabled"], [APP_DEV, KEYS])
        self.assertEqual(self.topo.meta["projects"], [APP_PROD, HOST])
        self.assertEqual(sum("Compute Engine API" in w for w in self.topo.meta["warnings"]), 1)
        # NCC switched off in a project is normal, not a warning.
        self.assertFalse([w for w in self.topo.meta["warnings"] if "NCC" in w])
        self.assertNotIn(f"gcp:project:{APP_DEV}", self.topo.nodes)

    def test_instances_only_on_request(self):
        self.assertFalse(self.topo.by_kind("gce-instance"))
        topo, _ = collect(include=["instances"])
        vm = topo.nodes[f"gcp:instance:{HOST}:us-east4-a:jump-1"]
        self.assertEqual(vm.parent, f"gcp:subnet:{HOST}:us-east4:prod-use4")
        self.assertEqual(vm.attrs["private_ips"], ["10.10.0.5"])
        self.assertEqual(vm.attrs["machine_type"], "e2-small")


class FailureTest(unittest.TestCase):
    def test_denied_listing_is_a_warning(self):
        denied = (403, {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "no"}})
        topo, _ = collect({f"{COMPUTE}/projects/{HOST}/aggregated/routers": denied})
        self.assertIn(HA_VPN, topo.nodes)
        self.assertTrue(any(w.startswith(f"gcp {HOST}: cannot list Cloud Routers")
                            for w in topo.meta["warnings"]))
        # The attachments still name the router: it becomes a placeholder.
        self.assertEqual(topo.nodes[ROUTER].attrs, {"external": True})
        self.assertIsNone(topo.nodes[ROUTER].parent)

    def test_denied_project_is_skipped(self):
        denied = (403, {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "no"}})
        topo, _ = collect({f"{COMPUTE}/projects/{APP_PROD}/global/networks": denied})
        self.assertTrue(any("project skipped" in w for w in topo.meta["warnings"]))
        self.assertNotIn(APP_PROD, topo.meta["projects"])

    def test_retries_server_errors(self):
        api, fake = gcp_fake.api()
        flaky = iter([(503, {}), (429, {})])
        original = fake.__call__

        def transport(url, params):
            if url.endswith("/global/networks") and HOST in url:
                for status in flaky:
                    return status
            return original(url, params)

        api._transport = transport
        topo = gcp.collect(api, [HOST])
        self.assertIn(NET, topo.nodes)

    def test_classic_vpn_peer_by_address(self):
        tunnel = {"name": "classic-1", "selfLink": f"{COMPUTE}/projects/{HOST}/regions/us-east4/vpnTunnels/classic-1",
                  "targetVpnGateway": f"{COMPUTE}/projects/{HOST}/regions/us-east4/targetVpnGateways/old-vpn",
                  "peerIp": "192.0.2.44", "status": "NO_INCOMING_PACKETS"}
        url = f"{COMPUTE}/projects/{HOST}/aggregated/vpnTunnels"
        topo, _ = collect({url: {"items": {"regions/us-east4": {"vpnTunnels": [tunnel]}}}}, projects=[HOST])
        peer = topo.nodes[f"gcp:external-vpn-gateway:{HOST}:peer-192.0.2.44"]
        self.assertEqual(peer.attrs["ip"], "192.0.2.44")


class MergeTest(unittest.TestCase):
    def device(self, name, *ips):
        t = Topology()
        t.add_node(f"net:device:{name}", "device", name,
                   interfaces=[{"name": f"Gi0/{i}", "addresses": [f"{ip}/29"]} for i, ip in enumerate(ips)])
        return t

    def test_stitches_vpn_and_interconnect(self):
        network, _ = collect()
        merged = merge([network, self.device("edge1", ONPREM_VPN_IP, IC_CUSTOMER_IP)])
        stitched = {l.attrs["rule"]: l for l in merged.links.values() if l.kind == "stitch"}
        self.assertEqual({stitched["gcp-vpn-peer"].source, stitched["gcp-vpn-peer"].target},
                         {ONPREM, "net:device:edge1"})
        self.assertEqual({stitched["gcp-interconnect"].source, stitched["gcp-interconnect"].target},
                         {DEDICATED, "net:device:edge1"})
        self.assertEqual(stitched["gcp-interconnect"].attrs["attachment"], "att-1")
        self.assertEqual(merged.meta["stitched"]["unmatched_customer_gateways"], [])

    def test_unmatched_external_gateway_is_reported(self):
        network, _ = collect()
        merged = merge([network, self.device("other", "192.0.2.1")])
        self.assertEqual(merged.meta["stitched"]["unmatched_customer_gateways"], [ONPREM])
        self.assertTrue(any("external VPN gateway" in w for w in merged.meta["warnings"]))

    def test_projects_nest_into_their_folders(self):
        api, _ = gcp_fake.api()
        org = gcp_org.collect(api)
        network, _ = collect()
        merged = merge([org, network])
        self.assertEqual(merged.validate(), [])
        self.assertEqual(merged.nodes[f"gcp:project:{HOST}"].parent, f"gcp:folder:{SHARED}")
        self.assertEqual(merged.nodes[NET].parent, f"gcp:project:{HOST}")
        self.assertEqual(merged.nodes[f"gcp:project:{HOST}"].attrs["shared_vpc"], "host")
        drawio = render(merged, "drawio")
        self.assertIn("2/2 tunnels up", drawio)
        self.assertIn("att-1", drawio)


class RenderTest(unittest.TestCase):
    def test_formats(self):
        topo, _ = collect()
        drawio = render(topo, "drawio")
        self.assertIn("image=data:image/svg+xml,", drawio)  # GCP icons
        self.assertIn("ASN 64512", drawio)
        svg = render(topo, "svg")
        self.assertIn("RTR", svg)
        self.assertIn("2/2 tunnels up", svg)
        mermaid = render(topo, "mermaid")
        self.assertIn("classDef gcp", mermaid)
        self.assertIn("graph topomap {", render(topo, "dot"))

    def test_key_project_icon(self):
        api, _ = gcp_fake.api()
        org = gcp_org.collect(api)
        from topomap.render import gcp_icons
        self.assertIn(gcp_icons.ICONS["key-management"][0], render(org, "drawio"))


class CliTest(unittest.TestCase):
    def test_collect_organization(self):
        api, _ = gcp_fake.api()
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "gcp.json")
            with mock.patch.object(gcp, "api_from_args", lambda args: api):
                status, _, err = run("collect", "gcp", "--organization", ORG, "-o", out)
            self.assertEqual(status, 0, err)
            topo = Topology.load(out)
            self.assertEqual(topo.meta["projects"], [APP_PROD, HOST])

    def test_nothing_to_collect(self):
        api, _ = gcp_fake.api()
        with mock.patch.object(gcp, "api_from_args", lambda args: api):
            status, _, err = run("collect", "gcp")
        self.assertEqual(status, 2)
        self.assertIn("--project, --folder or --organization", err)

    def test_missing_google_auth_is_explained(self):
        with mock.patch.dict(sys.modules, {"google": None, "google.auth": None}):
            status, _, err = run("collect", "gcp", "--project", HOST)
        self.assertEqual(status, 2)
        self.assertIn("pip install 'topomap[gcp]'", err)


if __name__ == "__main__":
    unittest.main()
