import ipaddress
import os
import random
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ipredact import Redactor, mask_len  # noqa: E402

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ipredact.py")


def ip_int(s):
    return int(ipaddress.IPv4Address(s))


class PrefixMapTest(unittest.TestCase):
    def setUp(self):
        self.r = Redactor("test-key")

    def test_bijection_and_reversible(self):
        outputs = {self.r.map16(v) for v in range(1 << 16)}
        self.assertEqual(len(outputs), 1 << 16)
        for v in range(0, 1 << 16, 97):
            self.assertEqual(self.r.unmap16(self.r.map16(v)), v)

    def test_both_octets_always_change(self):
        for v in range(1 << 16):
            out = self.r.map16(v)
            self.assertNotEqual(out >> 8, v >> 8)
            self.assertNotEqual(out & 0xFF, v & 0xFF)

    def test_prefix_preserving(self):
        rnd = random.Random(1)
        for _ in range(2000):
            a, b = rnd.randrange(1 << 16), rnd.randrange(1 << 16)
            shared = 16 - (a ^ b).bit_length()
            mapped = 16 - (self.r.map16(a) ^ self.r.map16(b)).bit_length()
            self.assertEqual(shared, mapped)

    def test_key_changes_mapping(self):
        other = Redactor("another-key")
        diffs = sum(self.r.map16(v) != other.map16(v) for v in range(1 << 16))
        self.assertGreater(diffs, 60000)


class TextTest(unittest.TestCase):
    def setUp(self):
        self.r = Redactor("test-key")

    def red(self, text):
        return self.r.redact_text(text)

    def test_last_two_octets_kept(self):
        out = self.red("host 10.20.30.40 up")
        ip = out.split()[1]
        self.assertTrue(ip.endswith(".30.40"))
        self.assertFalse(ip.startswith("10.20."))

    def test_consistent_and_same_subnet_stays_together(self):
        a, b = self.red("10.20.30.40").split(".")[:2], self.red("10.20.99.1").split(".")[:2]
        self.assertEqual(a, b)
        self.assertEqual(self.red("10.20.30.40"), self.red("10.20.30.40"))

    def test_cidr_containment_survives(self):
        net = self.red("172.16.0.0/12")
        self.assertTrue(net.endswith("/12"))
        network = ipaddress.ip_network(net)  # strict: must be a real network address
        for host in ("172.16.5.5", "172.31.255.1", "172.20.0.9"):
            self.assertIn(ipaddress.IPv4Address(self.red(host)), network)
        self.assertNotIn(ipaddress.IPv4Address(self.red("172.32.0.1")), network)

    def test_slash8_network_aligned(self):
        net = ipaddress.ip_network(self.red("10.0.0.0/8"))
        self.assertIn(ipaddress.IPv4Address(self.red("10.200.3.4")), net)

    def test_interface_cidr_not_realigned(self):
        out = self.red("10.5.0.1/8")
        self.assertTrue(out.endswith(".0.1/8"))

    def test_dotted_mask_and_wildcard(self):
        out = self.red("ip route 10.0.0.0 255.0.0.0 192.168.1.1")
        _, _, net, mask, gw = out.split()
        self.assertEqual(mask, "255.0.0.0")
        self.assertIn(ipaddress.IPv4Address(self.red("10.9.9.9")),
                      ipaddress.ip_network(f"{net}/{mask}"))
        self.assertTrue(gw.endswith(".1.1") and not gw.startswith("192.168."))

        out = self.red("network 10.0.0.0 0.255.255.255 area 0")
        self.assertIn("0.255.255.255", out)
        self.assertTrue(out.split()[1].endswith(".0.0.0"))

        out = self.red("network 10.0.0.0 mask 255.0.0.0")
        self.assertTrue(out.split()[1].endswith(".0.0.0"))

    def test_two_plain_addresses_both_redacted(self):
        out = self.red("range 10.1.1.1 10.1.1.200")
        self.assertNotIn("10.1.", out)

    def test_default_excludes(self):
        text = "lo 127.0.0.1 any 0.0.0.0 mask 255.255.255.0 md 169.254.169.254"
        self.assertEqual(self.red(text), text)

    def test_not_fooled_by_versions_or_longer_numbers(self):
        for text in ("v1.2.3.4.5", "1.2.3.4.5", "10.0.0.256", "OID 1.3.6.1.4.1.9"):
            self.assertEqual(self.red(text), text)

    def test_sentence_period(self):
        out = self.red("Reach it at 10.1.2.3.")
        self.assertTrue(out.endswith(".2.3.") and "10.1." not in out)

    def test_dashed_hostnames(self):
        out = self.red("ip-10-1-2-3.ec2.internal and 10.1.2.3")
        dashed, plain = out.split()[0], out.split()[-1]
        self.assertEqual(dashed.split(".")[0], "ip-" + plain.replace(".", "-"))
        self.assertEqual(Redactor("test-key", dashed=False)
                         .redact_text("ip-10-1-2-3"), "ip-10-1-2-3")

    def test_reverse_round_trip(self):
        text = ("interface 10.20.30.1/24\nroute 172.16.0.0/12 via 192.168.4.4\n"
                "network 10.0.0.0 0.255.255.255\nip-10-20-30-40.ec2.internal\n")
        red = self.red(text)
        self.assertNotEqual(red, text)
        self.assertEqual(Redactor("test-key").redact_text(red, reverse=True), text)

    def test_mask_len(self):
        self.assertEqual(mask_len(ip_int("255.255.0.0")), 16)
        self.assertEqual(mask_len(ip_int("0.0.255.255")), 16)
        self.assertEqual(mask_len(ip_int("255.255.255.255")), 32)
        self.assertIsNone(mask_len(ip_int("255.0.255.0")))
        self.assertIsNone(mask_len(ip_int("10.1.1.200")))


class CliTest(unittest.TestCase):
    def run_cli(self, *args, stdin=""):
        return subprocess.run([sys.executable, SCRIPT, *args], input=stdin,
                              capture_output=True, text=True, check=True)

    def test_stdin_with_key_is_deterministic_and_reversible(self):
        a = self.run_cli("-k", "abc", stdin="10.1.2.3\n").stdout
        b = self.run_cli("-k", "abc", stdin="10.1.2.3\n").stdout
        self.assertEqual(a, b)
        self.assertEqual(self.run_cli("-k", "abc", "-r", stdin=a).stdout, "10.1.2.3\n")

    def test_generated_key_saved_and_map_written(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "in.txt")
            with open(src, "w") as f:
                f.write("a 10.1.2.3\nb 10.1.9.9\n")
            keyf, mapf, outd = (os.path.join(d, n) for n in ("key", "map.csv", "out"))
            self.run_cli(src, "-d", outd, "--save-key", keyf, "--map-out", mapf)
            with open(os.path.join(outd, "in.txt")) as f:
                red = f.read()
            with open(mapf) as f:
                rows = f.read().splitlines()
            self.assertEqual(rows[0], "original,redacted")
            self.assertEqual(len(rows), 3)
            restored = self.run_cli("--key-file", keyf, "-r", stdin=red).stdout
            self.assertEqual(restored, "a 10.1.2.3\nb 10.1.9.9\n")

    def test_xlsx(self):
        try:
            import openpyxl
        except ImportError:
            self.skipTest("openpyxl not installed")
        with tempfile.TemporaryDirectory() as d:
            src, dst = os.path.join(d, "in.xlsx"), os.path.join(d, "out.xlsx")
            wb = openpyxl.Workbook()
            wb.active.append(["10.0.0.0/16", "vpc-main", 42])
            wb.save(src)
            self.run_cli(src, "-o", dst, "-k", "abc")
            cells = [c.value for c in openpyxl.load_workbook(dst).active[1]]
            self.assertTrue(cells[0].endswith(".0.0/16"))
            self.assertFalse(cells[0].startswith("10.0."))
            self.assertEqual(cells[1:], ["vpc-main", 42])


if __name__ == "__main__":
    unittest.main()
