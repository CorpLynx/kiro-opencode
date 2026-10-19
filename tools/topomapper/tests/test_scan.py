import argparse
import contextlib
import io
import os
import re
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from topomapper import cli  # noqa: E402
from topomapper.collectors import scan  # noqa: E402
from topomapper.model import Topology  # noqa: E402
from topomapper.render.labels import node_lines  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "scan")
DISCOVERY = os.path.join(FIXTURES, "discovery.xml")
PORTS = os.path.join(FIXTURES, "ports.xml")


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def parse(*argv):
    parser = argparse.ArgumentParser()
    scan.add_arguments(parser)
    return parser.parse_args(list(argv))


def collect(*argv, runner=None, arp=None):
    with mock.patch.object(scan, "default_gateways", return_value=[]), \
            mock.patch.object(scan, "arp_neighbours", return_value=arp or {}), \
            mock.patch.object(scan, "mac_vendors", return_value=lambda mac: ""):
        return scan.run(parse(*argv), runner=runner)


class FakeNmap:
    """Answers the discovery run, then the port run, from the fixtures."""

    def __init__(self):
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        return read(DISCOVERY if "-sn" in argv else PORTS)


class ParseTest(unittest.TestCase):
    def test_discovery(self):
        hosts, nets = scan.parse_nmap_xml(read(DISCOVERY))
        self.assertEqual(nets, ["192.0.2.0/24"])
        self.assertEqual([h.address for h in hosts],
                         ["192.0.2.1", "192.0.2.10", "192.0.2.20", "192.0.2.30"])  # .99 was down
        gw = hosts[0]
        self.assertEqual((gw.hostname, gw.mac, gw.mac_vendor),
                         ("gw.example.net", "00:00:5e:00:53:01", "Example Networks"))

    def test_ports(self):
        hosts, _ = scan.parse_nmap_xml(read(PORTS))
        server = hosts[1]
        self.assertEqual(server.ports, [
            {"port": 22, "protocol": "tcp", "service": "ssh", "product": "OpenSSH 9.8"},
            {"port": 8080, "protocol": "tcp", "service": "http", "product": "nginx"},
            {"port": 9999, "protocol": "tcp"},
        ])  # the closed port is left out
        self.assertEqual(server.os, "Linux 5.0 - 6.2")

    def test_tcpwrapped_is_not_a_name(self):
        text = read(PORTS).replace('name="ssh"', 'name="tcpwrapped"')
        server = scan.parse_nmap_xml(text)[0][1]
        self.assertEqual(server.ports[0], {"port": 22, "protocol": "tcp", "product": "OpenSSH 9.8"})

    def test_not_xml(self):
        for text in ("Starting Nmap", "<html/>"):
            with self.assertRaises(ValueError):
                scan.parse_nmap_xml(text)

    def test_merge_fills_in(self):
        hosts = scan.merge_hosts(scan.parse_nmap_xml(read(DISCOVERY))[0],
                                 scan.parse_nmap_xml(read(PORTS))[0])
        printer = next(h for h in hosts if h.address == "192.0.2.20")
        self.assertEqual(printer.mac_vendor, "Brother Industries")
        self.assertEqual([p["port"] for p in printer.ports], [631, 9100])


class ClassifyTest(unittest.TestCase):
    def kind(self, ports=(), vendor="", gateway=False):
        host = scan.ScanHost("192.0.2.5", mac_vendor=vendor,
                             ports=[{"port": p, "protocol": "tcp"} for p in ports])
        return scan.classify(host, gateway)

    def test_kinds(self):
        self.assertEqual(self.kind([53, 80], gateway=True), ("router", "gateway"))
        self.assertEqual(self.kind([9100]), ("unknown", "printer"))
        self.assertEqual(self.kind([80, 554]), ("unknown", "camera"))
        self.assertEqual(self.kind([5060]), ("phone", "voip"))
        self.assertEqual(self.kind([22, 8006]), ("server", "hypervisor"))
        self.assertEqual(self.kind([445, 5001]), ("server", "nas"))
        self.assertEqual(self.kind([62078]), ("unknown", "mobile"))
        self.assertEqual(self.kind([8009]), ("unknown", "media"))
        self.assertEqual(self.kind([22]), ("server", "server"))
        self.assertEqual(self.kind([3389]), ("unknown", "workstation"))
        self.assertEqual(self.kind([]), ("unknown", ""))
        self.assertEqual(self.kind([], vendor="eero inc."), ("wireless", "access point"))


class LookupTest(unittest.TestCase):
    def test_arp_cache_text(self):
        text = ("? (192.0.2.4) at 4:c9:d9:94:bb:d4 on en0 ifscope [ethernet]\n"
                "? (192.0.2.5) at (incomplete) on en0 ifscope [ethernet]\n"
                "? (192.0.2.255) at ff:ff:ff:ff:ff:ff on en0 ifscope [ethernet]\n")
        done = mock.Mock(stdout=text)
        with mock.patch("builtins.open", side_effect=OSError), \
                mock.patch.object(scan.subprocess, "run", return_value=done):
            self.assertEqual(scan.arp_neighbours(), {"192.0.2.4": "04:c9:d9:94:bb:d4"})

    def test_mac_vendors(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        os.makedirs(os.path.join(tmp, "bin"))
        os.makedirs(os.path.join(tmp, "share", "nmap"))
        with open(os.path.join(tmp, "share", "nmap", "nmap-mac-prefixes"), "w") as f:
            f.write("# comment\n00005E IANA\n00005E0053 Documentation\n")
        lookup = scan.mac_vendors(os.path.join(tmp, "bin", "nmap"))
        self.assertEqual(lookup("00:00:5e:00:53:01"), "IANA")   # longest prefix used is 9
        self.assertEqual(lookup("00:00:5e:10:00:01"), "IANA")
        self.assertEqual(lookup("02:00:00:00:00:01"), "")


class FromXmlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.topo = collect("--from-xml", DISCOVERY, "--from-xml", PORTS, "--gateway", "192.0.2.1")

    def test_valid(self):
        self.assertEqual(self.topo.validate(), [])
        self.assertEqual((self.topo.meta["collector"], self.topo.meta["networks"],
                          self.topo.meta["hosts"]), ("scan", ["192.0.2.0/24"], 4))
        self.assertNotIn("warnings", self.topo.meta)

    def test_nodes(self):
        devices = {n.label: n.attrs for n in self.topo.by_kind("device")}
        self.assertEqual(sorted(devices), ["192.0.2.20", "192.0.2.30", "gw", "lab-server"])
        self.assertEqual((devices["gw"]["role"], devices["gw"]["device_type"]), ("router", "gateway"))
        self.assertEqual(devices["lab-server"]["role"], "server")
        self.assertEqual(devices["192.0.2.20"]["device_type"], "printer")
        self.assertEqual(devices["lab-server"]["interfaces"],
                         [{"name": "", "addresses": ["192.0.2.10/24"]}])
        self.assertNotIn("open_ports", devices["192.0.2.30"])

    def test_segment(self):
        seg = "net:segment:192.0.2.0/24"
        self.assertIn(seg, self.topo.nodes)
        members = {l.source for l in self.topo.links.values() if l.target == seg}
        self.assertEqual(len(members), 4)

    def test_label(self):
        self.assertEqual(node_lines(self.topo.nodes["net:device:lab-server"]),
                         ["lab-server", "192.0.2.10", "22/ssh · 8080/http", "9999"])
        self.assertEqual(node_lines(self.topo.nodes["net:device:192.0.2.20"]),
                         ["192.0.2.20", "printer", "631/ipp · 9100/jetdirect"])

    def test_targets_override_networks(self):
        topo = collect("192.0.2.0/28", "--from-xml", DISCOVERY)
        self.assertEqual(topo.meta["networks"], ["192.0.2.0/28"])
        outside = topo.nodes["net:device:192.0.2.30"]
        self.assertFalse(topo.links_of(outside.id))   # .30 is not in the /28

    def test_placeholder_names(self):
        hosts = [scan.ScanHost("192.0.2.7", "unknown00005e005307.example.net")]
        topo = scan.hosts_to_topology(hosts, ["192.0.2.0/24"])
        node = topo.nodes["net:device:192.0.2.7"]
        self.assertEqual((node.label, node.attrs["hostname"]),
                         ("192.0.2.7", "unknown00005e005307.example.net"))

    def test_duplicate_names_fall_back_to_addresses(self):
        hosts = [scan.ScanHost("192.0.2.1", "box.example.net"),
                 scan.ScanHost("192.0.2.2", "box.example.org")]
        topo = scan.hosts_to_topology(hosts, ["192.0.2.0/24"])
        self.assertEqual(sorted(n.label for n in topo.by_kind("device")),
                         ["192.0.2.1", "192.0.2.2"])


class LiveScanTest(unittest.TestCase):
    def test_two_runs(self):
        nmap = FakeNmap()
        with mock.patch.object(scan, "is_root", return_value=True), \
                contextlib.redirect_stderr(io.StringIO()):
            topo = collect("192.0.2.0/24", "--exclude", "192.0.2.5", runner=nmap)
        discovery, ports = nmap.calls
        self.assertEqual(discovery[0], "-sn")
        self.assertEqual(discovery[-3:], ["--exclude", "192.0.2.5", "192.0.2.0/24"])
        self.assertIn("-sV", ports)
        self.assertIn("--version-light", ports)
        self.assertEqual(ports[ports.index("--top-ports") + 1], "1000")
        self.assertEqual(ports[-4:], ["192.0.2.1", "192.0.2.10", "192.0.2.20", "192.0.2.30"])
        self.assertEqual(len(topo.by_kind("device")), 4)
        self.assertNotIn("warnings", topo.meta)

    def test_options(self):
        nmap = FakeNmap()
        with mock.patch.object(scan, "is_root", return_value=True), \
                contextlib.redirect_stderr(io.StringIO()):
            collect("192.0.2.0/24", "--ports", "22,80", "--no-service-detection", "--no-dns",
                    "--os", "--timing", "2", runner=nmap)
        ports = nmap.calls[1]
        self.assertEqual(ports[ports.index("-p") + 1], "22,80")
        self.assertNotIn("-sV", ports)
        self.assertTrue({"-O", "-n", "-T2"} <= set(ports))

    def test_no_ports(self):
        nmap = FakeNmap()
        with mock.patch.object(scan, "is_root", return_value=False), \
                contextlib.redirect_stderr(io.StringIO()):
            topo = collect("192.0.2.0/24", "--no-ports", runner=nmap)
        self.assertEqual(len(nmap.calls), 1)
        self.assertIn("not running as root", topo.meta["warnings"][0])

    def test_save_xml(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        with contextlib.redirect_stderr(io.StringIO()):
            collect("192.0.2.0/24", "--save-xml", tmp, runner=FakeNmap())
        self.assertEqual(sorted(os.listdir(tmp)), ["discovery.xml", "ports.xml"])
        mode = stat.S_IMODE(os.stat(os.path.join(tmp, "ports.xml")).st_mode)
        self.assertEqual(mode, 0o600)

    def test_arp_cache_adds_missed_hosts(self):
        nmap = FakeNmap()
        arp = {"192.0.2.40": "00:00:5e:00:53:28",     # missed by the TCP pings
               "192.0.2.10": "00:00:5e:00:53:0a",     # already found
               "192.0.2.5": "00:00:5e:00:53:05",      # excluded
               "198.51.100.7": "00:00:5e:00:53:07"}   # not a target
        with mock.patch.object(scan, "is_root", return_value=False), \
                contextlib.redirect_stderr(io.StringIO()):
            topo = collect("192.0.2.0/24", "--exclude", "192.0.2.5", runner=nmap, arp=arp)
        self.assertEqual(nmap.calls[1][-5:], ["192.0.2.1", "192.0.2.10", "192.0.2.20",
                                              "192.0.2.30", "192.0.2.40"])
        self.assertEqual(topo.nodes["net:device:192.0.2.40"].attrs["mac"], "00:00:5e:00:53:28")
        self.assertNotIn("net:device:192.0.2.5", topo.nodes)
        self.assertNotIn("net:device:198.51.100.7", topo.nodes)

    def test_root_skips_arp_cache(self):
        nmap = FakeNmap()
        with mock.patch.object(scan, "is_root", return_value=True), \
                contextlib.redirect_stderr(io.StringIO()):
            topo = collect("192.0.2.0/24", runner=nmap, arp={"192.0.2.40": "00:00:5e:00:53:28"})
        self.assertNotIn("net:device:192.0.2.40", topo.nodes)

    def test_os_needs_root(self):
        with mock.patch.object(scan, "is_root", return_value=False):
            with self.assertRaisesRegex(ValueError, "root"):
                collect("192.0.2.0/24", "--os", runner=FakeNmap())


class TargetTest(unittest.TestCase):
    def test_local_space(self):
        self.assertEqual(scan.check_targets(["10.1.2.3", "192.168.1.0/24", "100.64.0.0/24",
                                             "fe80::/120", "fd00::1"], False),
                         ["10.1.2.3/32", "192.168.1.0/24", "100.64.0.0/24", "fe80::/120",
                          "fd00::1/128"])

    def test_public_refused(self):
        with self.assertRaisesRegex(ValueError, "--allow-public"):
            scan.check_targets(["8.8.8.0/24"], False)
        self.assertEqual(scan.check_targets(["8.8.8.8"], True), ["8.8.8.8/32"])

    def test_bad_and_huge(self):
        with self.assertRaisesRegex(ValueError, "not an address"):
            scan.check_targets(["example.net"], False)
        with self.assertRaisesRegex(ValueError, "split it up"):
            scan.check_targets(["10.0.0.0/8"], False)

    def test_needs_something(self):
        with self.assertRaisesRegex(ValueError, "TARGET"):
            collect()

    def test_nmap_missing(self):
        with mock.patch.object(scan, "find_nmap", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "nmap not found"):
                collect("192.0.2.0/24")

    def test_find_nmap_outside_path(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        fake = os.path.join(tmp, "nmap")
        with open(fake, "w") as f:
            f.write("#!/bin/sh\n")
        os.chmod(fake, 0o755)
        with mock.patch.object(scan.shutil, "which", return_value=None), \
                mock.patch.object(scan, "_NMAP_DIRS", (tmp,)):
            self.assertEqual(scan.find_nmap("nmap"), fake)
            self.assertIsNone(scan.find_nmap("no-such-nmap"))


class LabelTest(unittest.TestCase):
    def test_many_ports_wrap_and_cap(self):
        from topomapper.render import labels
        ports = [{"port": n, "protocol": "tcp", "service": "http"} for n in range(8000, 8014)]
        topo = scan.hosts_to_topology([scan.ScanHost("192.0.2.9", ports=ports)], ["192.0.2.0/24"])
        node = topo.nodes["net:device:192.0.2.9"]
        lines = labels.node_lines(node)
        self.assertEqual(lines[2], "8000/http · 8001/http")
        self.assertEqual(lines[-1], "+4 more")
        self.assertIn("8013/http", labels.tooltip(node))     # the tooltip has them all

    def test_warns_when_no_service_is_named(self):
        text = re.sub(r'<service name="[^"]+"', '<service name="tcpwrapped"', read(PORTS))
        with mock.patch.object(scan, "is_root", return_value=True), \
                contextlib.redirect_stderr(io.StringIO()):
            topo = collect("192.0.2.0/24", runner=lambda argv: read(DISCOVERY) if "-sn" in argv
                           else text)
        self.assertTrue(any("named none" in w for w in topo.meta.get("warnings", [])))


class RunnerTest(unittest.TestCase):
    """nmap_runner against a stand-in program that prints progress and writes XML."""

    def fake(self, body):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        path = os.path.join(tmp, "nmap")
        with open(path, "w") as f:
            f.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)
        return path

    def test_progress_and_xml(self):
        path = self.fake(
            'while [ "$1" != "-oX" ]; do shift; done; out="$2"\n'
            'echo "Starting Nmap 7.95"\n'
            'echo "Stats: 0:00:05 elapsed; 0 hosts completed (0 up)"\n'
            'echo "Ping Scan Timing: About 40.00% done; ETC: 12:00 (0:00:03 remaining)"\n'
            'echo "Nmap scan report for 192.0.2.1"\n'
            'echo "<nmaprun/>" > "$out"\n')
        lines = []
        xml = scan.nmap_runner(path, lines.append)(["-sn", "192.0.2.0/24"])
        self.assertEqual(xml.strip(), "<nmaprun/>")
        self.assertEqual(lines, ["nmap: Ping Scan Timing: About 40.00% done; ETC: 12:00 (0:00:03 remaining)",
                                 "nmap: Nmap scan report for 192.0.2.1"])   # no banner, no Stats

    def test_failure_names_the_cause(self):
        path = self.fake('echo "You requested a scan type which requires root privileges."\nexit 1\n')
        with self.assertRaisesRegex(RuntimeError, "requires root"):
            scan.nmap_runner(path)(["-sS", "192.0.2.1"])


class CliTest(unittest.TestCase):
    def test_render(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        out = os.path.join(tmp, "lan.json")
        with mock.patch.object(scan, "default_gateways", return_value=["192.0.2.1"]), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(["collect", "scan", "--from-xml", DISCOVERY,
                                       "--from-xml", PORTS, "-o", out]), 0)
        topo = Topology.load(out)
        self.assertEqual(topo.nodes["net:device:gw"].attrs["role"], "router")
        for ext in ("svg", "drawio", "mmd", "dot"):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["render", out, "-o", os.path.join(tmp, f"lan.{ext}")]),
                                 0)
        with open(os.path.join(tmp, "lan.svg"), encoding="utf-8") as f:
            self.assertIn("lab-server", f.read())


if __name__ == "__main__":
    unittest.main()
