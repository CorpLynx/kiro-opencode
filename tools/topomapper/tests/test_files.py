import argparse
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from topomapper import cli  # noqa: E402
from topomapper.collectors import files  # noqa: E402
from topomapper.model import Topology  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "devices")
SITES = os.path.join(FIXTURES, "sites.csv")


def collect(*argv):
    parser = argparse.ArgumentParser()
    files.add_arguments(parser)
    return files.run(parser.parse_args(list(argv)))


def dev(name):
    return f"net:device:{name}"


def cables(topo):
    """{frozenset of (device, port)} for every physical link."""
    return {frozenset({(l.source[11:], l.attrs.get("source_port", "")),
                       (l.target[11:], l.attrs.get("target_port", ""))}): l
            for l in topo.links.values() if l.kind == "physical"}


class LabTest(unittest.TestCase):
    """The fixture lab, collected as a whole: see fixtures/devices/README.md."""

    @classmethod
    def setUpClass(cls):
        cls.topo = collect(FIXTURES, "--site-map", SITES, "--l3-exclude", "192.168.0.0/24")

    def test_valid(self):
        self.assertEqual(self.topo.validate(), [])
        self.assertNotIn("warnings", self.topo.meta)
        meta = self.topo.meta
        self.assertEqual((meta["collector"], meta["files"], meta["devices"]), ("files", 7, 6))
        self.assertRegex(meta["collected_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

    def test_devices(self):
        collected = {n.label for n in self.topo.by_kind("device") if not n.attrs.get("discovered")}
        discovered = {n.label for n in self.topo.by_kind("device") if n.attrs.get("discovered")}
        self.assertEqual(collected, {"edge1", "core1", "core2", "dist1", "leaf1", "wan1"})
        self.assertEqual(discovered, {"ap1", "sep00005e0053a0", "srv1"})
        roles = {n.label: n.attrs["role"] for n in self.topo.by_kind("device")}
        self.assertEqual(roles, {"edge1": "router", "core1": "switch", "core2": "switch",
                                 "dist1": "switch", "leaf1": "switch", "wan1": "router",
                                 "ap1": "wireless", "sep00005e0053a0": "phone", "srv1": "server"})
        edge = self.topo.nodes[dev("edge1")].attrs
        self.assertEqual((edge["vendor"], edge["platform"], edge["mgmt_ip"], edge["serial"]),
                         ("juniper", "juniper_junos", "192.168.0.1", "DX0000AF0042"))
        self.assertEqual(edge["sources"], [os.path.join(FIXTURES, "edge1.txt"),
                                           os.path.join(FIXTURES, "configs", "edge1.conf")])
        isp = next(i for i in edge["interfaces"] if i["name"] == "xe-0/1/7")
        self.assertEqual(isp["vrf"], "INTERNET")       # from the config, merged into the capture
        self.assertEqual(self.topo.nodes[dev("wan1")].attrs["model"], "ISR4331/K9")  # from dist1's CDP

    def test_cables(self):
        expected = {
            (("core1", "Ethernet1/49"), ("edge1", "xe-0/1/0")): ["lldp"],
            (("core2", "Ethernet1/49"), ("edge1", "xe-0/1/1")): ["lldp"],
            (("core1", "Ethernet1/53"), ("core2", "Ethernet1/53")): ["cdp", "lldp"],
            (("core1", "Ethernet1/1"), ("dist1", "TenGigabitEthernet1/1/1")): ["cdp", "lldp"],
            (("core2", "Ethernet1/1"), ("dist1", "TenGigabitEthernet1/1/2")): ["cdp", "lldp"],
            (("core1", "Ethernet1/2"), ("leaf1", "Ethernet49/1")): ["lldp"],
            (("core2", "Ethernet1/2"), ("leaf1", "Ethernet50/1")): ["lldp"],
            (("dist1", "GigabitEthernet1/0/48"), ("wan1", "GigabitEthernet0/0/1")): ["cdp"],
            (("ap1", "GigabitEthernet0"), ("dist1", "GigabitEthernet1/0/1")): ["cdp", "lldp"],
            (("dist1", "GigabitEthernet1/0/10"), ("sep00005e0053a0", "Port 1")): ["cdp", "lldp"],
            (("leaf1", "Ethernet1"), ("srv1", "eth0")): ["lldp"],
        }
        got = cables(self.topo)
        self.assertEqual(set(got), {frozenset(k) for k in expected})   # one link per cable
        for ends, protocols in expected.items():
            self.assertEqual(sorted(got[frozenset(ends)].attrs["protocols"]), protocols, ends)
        edge_core = got[frozenset({("core1", "Ethernet1/49"), ("edge1", "xe-0/1/0")})]
        self.assertEqual(edge_core.attrs["subnets"], ["10.0.0.0/31", "2001:db8:0:1::/64"])

    def test_l3_and_segments(self):
        l3 = {l.attrs["subnet"]: (l.source, l.target) for l in self.topo.links.values() if l.kind == "l3"}
        self.assertEqual(l3, {"10.0.0.20/30": (dev("core1"), dev("core2")),
                              "172.16.0.0/30": (dev("edge1"), dev("wan1"))})
        segments = {n.label for n in self.topo.by_kind("segment")}
        self.assertEqual(segments, {"10.20.100.0/24", "2001:db8:20:100::/64"})
        members = {l.source for l in self.topo.links.values()
                   if l.kind == "segment-member" and l.target == "net:segment:10.20.100.0/24"}
        self.assertEqual(members, {dev("core1"), dev("core2"), dev("dist1")})

    def test_sites(self):
        parents = {n.label: n.parent for n in self.topo.by_kind("device")}
        self.assertEqual(parents["core2"], "net:site:dc1")          # wildcard row
        self.assertEqual(parents["wan1"], "net:site:dc1-wan")
        self.assertIsNone(parents["ap1"])                           # discovered: no site
        self.assertEqual(self.topo.nodes["net:site:dc1"].label, "dc1")


class OptionsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir)

    def write(self, name, data):
        path = os.path.join(self.dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data if isinstance(data, bytes) else data.encode())
        return path

    def test_management_segment_and_no_l3(self):
        topo = collect(FIXTURES)
        self.assertIn("net:segment:192.168.0.0/24", topo.nodes)
        topo = collect(FIXTURES, "--no-l3")
        self.assertFalse([l for l in topo.links.values() if l.kind in ("l3", "segment-member")])
        self.assertEqual(len([l for l in topo.links.values() if l.kind == "physical"]), 11)

    def test_site_for_everything(self):
        topo = collect(os.path.join(FIXTURES, "configs"), "--site", "Lab")
        self.assertEqual({n.parent for n in topo.by_kind("device")}, {"net:site:lab"})

    def test_walk_skips_and_warns(self):
        shutil.copy(os.path.join(FIXTURES, "dist1.txt"), os.path.join(self.dir, "dist1.txt"))
        self.write("notes.txt", "remember to renew the ISP contract\n")
        self.write("image.bin", b"\x89PNG\r\n\x1a\n\0\0\0")
        self.write(".hidden/core9.txt", "core9#show version\ncore9 uptime is 1 week\n")
        self.write("README.md", "# backups\n")
        topo = collect(self.dir)
        self.assertEqual(topo.meta["files"], 1)
        self.assertEqual(topo.meta["warnings"],
                         [f"{os.path.join(self.dir, 'notes.txt')}: no recognisable command output "
                          "or configuration"])
        self.assertNotIn(dev("core9"), topo.nodes)
        topo = collect(os.path.join(self.dir, "image.bin"))
        self.assertIn("binary", topo.meta["warnings"][0])

    def test_glob(self):
        topo = collect(FIXTURES, "--glob", "*.txt")
        self.assertEqual(topo.meta["files"], 4)    # edge1, core1, dist1, leaf1 captures
        self.assertNotIn("wan1", {n.label for n in topo.by_kind("device")
                                  if not n.attrs.get("discovered")})

    def test_platform_hint(self):
        path = self.write("sw5.txt", "Vlan5 is up, line protocol is up\n  Internet address is 10.5.0.1/24\n")
        node = collect(path, "--platform", "arista_eos").nodes[dev("sw5")]
        self.assertEqual((node.attrs["platform"], node.attrs["vendor"]), ("arista_eos", "arista"))

    def test_bad_arguments(self):
        with self.assertRaises(ValueError):
            collect(os.path.join(self.dir, "missing"))
        with self.assertRaises(ValueError):
            collect(FIXTURES, "--l3-exclude", "10.0.0.0/33")

    def test_site_map_format(self):
        csv = self.write("sites.csv", "# comment\nHOSTNAME,SITE\nCore1.example.net,DC1\n\nedge*,edge\n")
        self.assertEqual(files.read_site_map(csv), {"core1": "DC1", "edge*": "edge"})
        self.assertEqual(files.site_for("edge1", files.read_site_map(csv)), "edge")


class CliTest(unittest.TestCase):
    def test_collect_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "lab.json")
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                status = cli.main(["collect", "files", FIXTURES, "--site-map", SITES,
                                   "--l3-exclude", "192.168.0.0/24", "-o", out])
            self.assertEqual((status, stderr.getvalue()), (0, ""))
            topo = Topology.load(out)
        self.assertEqual(topo.validate(), [])
        self.assertEqual(topo.meta["devices"], 6)
        self.assertEqual(len([l for l in topo.links.values() if l.kind == "physical"]), 11)

    def test_bad_path_is_an_error(self):
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            status = cli.main(["collect", "files", os.path.join(FIXTURES, "nope")])
        self.assertEqual(status, 2)
        self.assertIn("no such file or directory", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
