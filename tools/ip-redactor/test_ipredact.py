import ipaddress
import os
import random
import stat
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from ipredact import DEFAULT_EXCLUDES, LINK_LOCAL16, Redactor, fmt_v6, mask_len  # noqa: E402

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ipredact.py")
KEY = "test-key-0123456789"


def ip_int(s):
    return int(ipaddress.ip_address(s))


class V4MapTest(unittest.TestCase):
    def setUp(self):
        self.r = Redactor(KEY)

    def v4(self, value, reverse=False):
        return self.r._map_v4_int(value, reverse)

    def test_bijection_over_all_prefixes_and_reversible(self):
        prefixes = [p for p in range(1 << 16) if p != LINK_LOCAL16]  # 169.254 is excluded
        outputs = {self.v4(p << 16) >> 16 for p in prefixes}
        self.assertEqual(len(outputs), len(prefixes))
        self.assertNotIn(LINK_LOCAL16, outputs)
        for p in prefixes:
            self.assertEqual(self.v4(self.v4(p << 16), reverse=True), p << 16)

    def test_link_local_kept_when_not_excluded(self):
        r = Redactor(KEY, excludes=[])
        outputs = {r._map_v4_int(p << 16, False) >> 16 for p in range(1 << 16)}
        self.assertEqual(len(outputs), 1 << 16)

    def test_both_octets_always_change_and_never_special(self):
        for p in range(1 << 16):
            o1, o2 = p >> 8, p & 0xFF
            if o1 in (0, 127, 255):
                continue
            out = self.v4(p << 16)
            self.assertNotEqual(out >> 24, o1)
            self.assertNotEqual((out >> 16) & 0xFF, o2)
            self.assertNotIn(out >> 24, (0, 127, 255))

    def test_prefix_preserving_at_slash8_and_longer(self):
        rnd = random.Random(1)
        for _ in range(3000):
            a, b = rnd.randrange(1, 127) << 24, 0
            a |= rnd.randrange(1 << 24)
            b = a ^ (1 << rnd.randrange(24))  # same first octet, differ somewhere below
            shared = 32 - (a ^ b).bit_length()
            self.assertEqual(32 - (self.v4(a) ^ self.v4(b)).bit_length(), shared)

    def test_key_changes_mapping(self):
        other = Redactor("another-key-0123456789")
        diffs = sum(self.v4(p << 16) != other._map_v4_int(p << 16, False) for p in range(1 << 16))
        self.assertGreater(diffs, 60000)


class V6MapTest(unittest.TestCase):
    def setUp(self):
        self.r = Redactor(KEY)

    def test_bijection_and_reversible(self):
        r16 = Redactor(KEY, v6_bits=16)
        outs = {r16._map_v6_int(p << 112, False) for p in range(1 << 16)}
        self.assertEqual(len(outs), 1 << 16)
        rnd = random.Random(2)
        for _ in range(500):
            v = rnd.getrandbits(128)
            self.assertEqual(self.r._map_v6_int(self.r._map_v6_int(v, False), True), v)

    def test_type_preserved_and_hextets_change(self):
        rnd = random.Random(3)
        glob = ipaddress.ip_network("2000::/3")
        ula = ipaddress.ip_network("fc00::/7")
        for net in (glob, ula):
            for _ in range(500):
                v = int(net.network_address) | rnd.getrandbits(128 - net.prefixlen)
                out = self.r._map_v6_int(v, False)
                self.assertIn(ipaddress.IPv6Address(out), net)
                for h in range(3):
                    shift = 112 - 16 * h
                    self.assertNotEqual((out >> shift) & 0xFFFF, (v >> shift) & 0xFFFF)
                self.assertEqual(out & ((1 << 80) - 1), v & ((1 << 80) - 1))

    def test_prefix_preserving(self):
        rnd = random.Random(4)
        for _ in range(2000):
            a = (0x2 << 125) | rnd.getrandbits(125)
            b = a ^ (1 << rnd.randrange(80, 125))
            shared = 128 - (a ^ b).bit_length()
            mapped = 128 - (self.r._map_v6_int(a, False) ^ self.r._map_v6_int(b, False)).bit_length()
            self.assertEqual(shared, mapped)


class TextTest(unittest.TestCase):
    def setUp(self):
        self.r = Redactor(KEY)

    def red(self, text):
        return self.r.redact_text(text)

    # IPv4 -------------------------------------------------------------

    def test_last_two_octets_kept(self):
        ip = self.red("host 10.20.30.40 up").split()[1]
        self.assertTrue(ip.endswith(".30.40"))
        self.assertFalse(ip.startswith("10.") or ".20.30" in ip)

    def test_consistent_and_same_subnet_stays_together(self):
        self.assertEqual(self.red("10.20.30.40").split(".")[:2], self.red("10.20.99.1").split(".")[:2])
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

    def test_short_prefixes_left_alone_unless_host(self):
        self.assertEqual(self.red("128.0.0.0/1 10.0.0.0/7"), "128.0.0.0/1 10.0.0.0/7")
        self.assertNotIn("10.1.1.1", self.red("10.1.1.1 128.0.0.0"))

    def test_interface_cidr_not_realigned(self):
        self.assertTrue(self.red("10.5.0.1/8").endswith(".0.1/8"))

    def test_dotted_mask_and_wildcard(self):
        out = self.red("ip route 10.0.0.0 255.0.0.0 192.168.1.1")
        _, _, net, mask, gw = out.split()
        self.assertEqual(mask, "255.0.0.0")
        self.assertIn(ipaddress.IPv4Address(self.red("10.9.9.9")), ipaddress.ip_network(f"{net}/{mask}"))
        self.assertTrue(gw.endswith(".1.1") and not gw.startswith("192.168."))
        out = self.red("network 10.0.0.0 0.255.255.255 area 0")
        self.assertIn("0.255.255.255", out)
        self.assertTrue(out.split()[1].endswith(".0.0.0"))
        self.assertTrue(self.red("network 10.0.0.0 mask 255.0.0.0").split()[1].endswith(".0.0.0"))

    def test_adjacent_and_punctuated_addresses(self):
        for text in ("range 10.1.1.1 10.1.1.200", "range 10.0.0.1-10.0.0.9",
                     "host:10.1.2.3:443", "ip=10.1.2.3,", "Reach it at 10.1.2.3."):
            self.assertNotIn("10.", self.red(text), text)

    def test_zero_padded_octets(self):
        out = self.red("010.042.007.001")
        self.assertRegex(out, r"^\d{3}\.\d{3}\.007\.001$")
        self.assertEqual(out, self.red("10.42.7.1").split(".")[0].zfill(3) + "."
                         + self.red("10.42.7.1").split(".")[1].zfill(3) + ".007.001")

    def test_default_excludes(self):
        text = "lo 127.0.0.1 any 0.0.0.0 mask 255.255.255.0 md 169.254.169.254 ::1 fe80::1%eth0 ff02::1"
        self.assertEqual(self.red(text), text)

    def test_not_fooled(self):
        for text in ("v1.2.3.4.5", "1.2.3.4.5", "10.0.0.256", "OID 1.3.6.1.4.1.9",
                     "mac 00:1a:2b:3c:4d:5e", "at 12:34:56.789", "std::vector a::b dead::beef"):
            self.assertEqual(self.red(text), text, text)

    def test_dashed_hostnames(self):
        out = self.red("ip-10-1-2-3.ec2.internal and 10.1.2.3")
        dashed, plain = out.split()[0], out.split()[-1]
        self.assertEqual(dashed.split(".")[0], "ip-" + plain.replace(".", "-"))
        self.assertEqual(Redactor(KEY, dashed=False).redact_text("ip-10-1-2-3"), "ip-10-1-2-3")

    def test_reverse_dns_v4(self):
        host = self.red("10.42.7.15").split(".")
        self.assertEqual(self.red("15.7.42.10.in-addr.arpa."), ".".join(reversed(host)) + ".in-addr.arpa.")
        self.assertEqual(self.red("42.10.in-addr.arpa"), f"{host[1]}.{host[0]}.in-addr.arpa")
        self.assertEqual(self.red("10.in-addr.arpa"), f"{host[0]}.in-addr.arpa")

    # IPv6 -------------------------------------------------------------

    def test_v6_basic(self):
        out = self.red("2001:db8:abcd:12::1")
        self.assertTrue(out.endswith(":12::1"))
        self.assertFalse(out.startswith("2001:"))
        self.assertIn(ipaddress.ip_address(out), ipaddress.ip_network("2000::/3"))

    def test_v6_subnets_and_containment(self):
        net = ipaddress.ip_network(self.red("2001:db8:abcd::/48"))
        for host in ("2001:db8:abcd:12::1", "2001:db8:abcd:ffff:1:2:3:4"):
            self.assertIn(ipaddress.ip_address(self.red(host)), net)
        self.assertNotIn(ipaddress.ip_address(self.red("2001:db8:abce::1")), net)
        net32 = ipaddress.ip_network(self.red("2001:db8::/32"))  # strict
        self.assertTrue(net.subnet_of(net32))

    def test_v6_formats_preserved(self):
        full = self.red("2001:0db8:abcd:0012:0000:0000:0000:0001")
        self.assertRegex(full, r"^([0-9a-f]{4}:){7}[0-9a-f]{4}$")
        self.assertEqual(ipaddress.ip_address(full), ipaddress.ip_address(self.red("2001:db8:abcd:12::1")))
        upper = self.red("2001:DB8:ABCD:12::1")
        self.assertEqual(upper, self.red("2001:db8:abcd:12::1").upper())

    def test_v6_zone_brackets_and_ula(self):
        out = self.red("http://[2600:1f18:4a2b:7c01::25]:8443/")
        self.assertTrue(out.startswith("http://[") and out.endswith("::25]:8443/"))
        self.assertNotIn("2600:1f18", out)
        ula = self.red("fd12:3456:789a:1::10")
        self.assertTrue(ula.startswith("fd") and ula.endswith(":1::10"))
        self.assertNotEqual(ula, "fd12:3456:789a:1::10")

    def test_v6_embedded_v4_consistent(self):
        v4 = self.red("10.42.7.15")
        self.assertEqual(self.red("::ffff:10.42.7.15"), f"::ffff:{v4}")
        self.assertEqual(self.red("64:ff9b::10.42.7.15"), f"64:ff9b::{v4}")

    def test_reverse_dns_v6(self):
        host = ipaddress.ip_address(self.red("2001:db8:abcd:12::1"))
        expected = ".".join(reversed(host.exploded.replace(":", "")[:16])) + ".ip6.arpa"
        self.assertEqual(self.red("2.1.0.0.d.c.b.a.8.b.d.0.1.0.0.2.ip6.arpa"), expected)

    def test_v6_bits_option(self):
        r64 = Redactor(KEY, v6_bits=64)
        out = r64.redact_text("2001:db8:abcd:12::1")
        self.assertTrue(out.endswith("::1") and ":12::" not in out)

    def test_fmt_v6(self):
        self.assertEqual(fmt_v6(0, "::"), "::")
        self.assertEqual(fmt_v6(ip_int("1:0:0:2::"), "a::"), "1:0:0:2::")
        self.assertEqual(fmt_v6(ip_int("1:0:2:0:0:3:0:0"), "a::b"), "1:0:2::3:0:0")
        self.assertEqual(fmt_v6(ip_int("::ffff:1.2.3.4"), "::ffff:9.9.9.9"), "::ffff:1.2.3.4")

    # both -------------------------------------------------------------

    def test_reverse_round_trip(self):
        text = ("interface 10.20.30.1/24\nroute 172.16.0.0/12 via 192.168.4.4\n"
                "network 10.0.0.0 0.255.255.255\nip-10-20-30-40.ec2.internal\n"
                "padded 010.042.007.001\nv6 2001:db8:abcd:12::1/64 [2600::1]:80 ::ffff:10.1.2.3\n"
                "full 2001:0DB8:ABCD:0012:0000:0000:0000:0001 ula fd00:1:2::/48\n"
                "15.7.42.10.in-addr.arpa. 2.1.0.0.d.c.b.a.8.b.d.0.1.0.0.2.ip6.arpa.\n")
        red = self.red(text)
        self.assertNotEqual(red, text)
        self.assertEqual(Redactor(KEY).redact_text(red, reverse=True), text)

    def test_outputs_avoid_default_excludes(self):
        rnd = random.Random(5)
        for _ in range(3000):
            self.red(str(ipaddress.IPv4Address(rnd.randrange(1 << 32))))
            self.red(str(ipaddress.IPv6Address((0x2 << 125) | rnd.getrandbits(125))))
        excl = [ipaddress.ip_network(n) for n in DEFAULT_EXCLUDES]
        for out in self.r.seen.values():
            self.assertFalse(any(out.version == n.version and out in n for n in excl), out)

    def test_mask_len(self):
        self.assertEqual(mask_len(ip_int("255.255.0.0")), 16)
        self.assertEqual(mask_len(ip_int("0.0.255.255")), 16)
        self.assertEqual(mask_len(ip_int("255.255.255.255")), 32)
        self.assertIsNone(mask_len(ip_int("255.0.255.0")))
        self.assertIsNone(mask_len(ip_int("10.1.1.200")))


class CliTest(unittest.TestCase):
    def run_cli(self, *args, stdin=b"", check=True):
        return subprocess.run([sys.executable, SCRIPT, *args], input=stdin,
                              capture_output=True, check=check)

    def test_stdin_with_key_is_deterministic_and_reversible(self):
        a = self.run_cli("-k", KEY, stdin=b"10.1.2.3 2001:db8::1\n").stdout
        self.assertEqual(a, self.run_cli("-k", KEY, stdin=b"10.1.2.3 2001:db8::1\n").stdout)
        self.assertEqual(self.run_cli("-k", KEY, "-r", stdin=a).stdout, b"10.1.2.3 2001:db8::1\n")

    def test_short_key_warns(self):
        self.assertIn(b"shorter than", self.run_cli("-k", "abc", stdin=b"x").stderr)

    def test_generated_key_saved_and_map_written_privately(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "in.txt")
            with open(src, "w") as f:
                f.write("a 10.1.2.3\nb 10.1.9.9\nc 2001:db8::1\n")
            keyf, mapf, outd = (os.path.join(d, n) for n in ("key", "map.csv", "out"))
            self.run_cli(src, "-d", outd, "--save-key", keyf, "--map-out", mapf)
            for p in (keyf, mapf):
                self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)
            with open(os.path.join(outd, "in.txt"), "rb") as f:
                red = f.read()
            with open(mapf) as f:
                rows = f.read().splitlines()
            self.assertEqual(rows[0], "original,redacted")
            self.assertEqual(len(rows), 4)
            restored = self.run_cli("--key-file", keyf, "-r", stdin=red).stdout
            self.assertEqual(restored, b"a 10.1.2.3\nb 10.1.9.9\nc 2001:db8::1\n")

    def test_directory_binary_utf16_and_in_place(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "src")
            os.makedirs(os.path.join(src, "sub"))
            with open(os.path.join(src, "sub", "a.cfg"), "w") as f:
                f.write("10.1.2.3\n")
            with open(os.path.join(src, "w.txt"), "wb") as f:
                f.write(b"\xff\xfe" + "host 10.1.2.3\r\n".encode("utf-16-le"))
            with open(os.path.join(src, "blob.bin"), "wb") as f:
                f.write(b"\x00\x0a\x01\x02\x03 10.1.2.3")
            res = self.run_cli(src, "-d", os.path.join(d, "out"), "-k", KEY, check=False)
            self.assertEqual(res.returncode, 1)  # the binary file was skipped
            self.assertIn(b"blob.bin", res.stderr)
            with open(os.path.join(d, "out", "sub", "a.cfg")) as f:
                self.assertNotIn("10.1.", f.read())
            with open(os.path.join(d, "out", "w.txt"), "rb") as f:
                data = f.read()
            self.assertTrue(data.startswith(b"\xff\xfe"))
            self.assertNotIn("10.1.", data[2:].decode("utf-16-le"))
            self.assertFalse(os.path.exists(os.path.join(d, "out", "blob.bin")))

            target = os.path.join(src, "sub", "a.cfg")
            os.chmod(target, 0o640)
            self.run_cli(target, "-i", "-k", KEY)
            self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o640)
            self.assertEqual([n for n in os.listdir(os.path.dirname(target)) if n.startswith(".ipredact")], [])

    def test_refuses_to_overwrite_input_or_collide(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = os.path.join(d, "x", "f.txt"), os.path.join(d, "y", "f.txt")
            for p in (a, b):
                os.makedirs(os.path.dirname(p))
                with open(p, "w") as f:
                    f.write("10.1.2.3\n")
            self.assertNotEqual(self.run_cli(a, "-o", a, "-k", KEY, check=False).returncode, 0)
            self.assertNotEqual(self.run_cli(a, b, "-d", d, "-k", KEY, check=False).returncode, 0)
            with open(a) as f:
                self.assertEqual(f.read(), "10.1.2.3\n")

    def test_xlsx(self):
        try:
            import openpyxl
            from openpyxl.comments import Comment
        except ImportError:
            self.skipTest("openpyxl not installed")
        with tempfile.TemporaryDirectory() as d:
            src, dst = os.path.join(d, "in.xlsx"), os.path.join(d, "out.xlsx")
            wb = openpyxl.Workbook()
            ws = wb.active
            ws.title = "10.1.0.0"
            ws.append(["10.0.0.0/16", "vpc-main", 42, "2001:db8:abcd::/48"])
            ws["B1"].comment = Comment("owner of 10.0.4.4", "me")
            ws["B1"].hyperlink = "https://10.0.4.4/ui"
            wb.save(src)
            self.run_cli(src, "-o", dst, "-k", KEY)
            ws = openpyxl.load_workbook(dst).active
            cells = [c.value for c in ws[1]]
            self.assertTrue(cells[0].endswith(".0.0/16") and not cells[0].startswith("10.0."))
            self.assertEqual(cells[1:3], ["vpc-main", 42])
            self.assertFalse(cells[3].startswith("2001:db8"))
            self.assertFalse(ws.title.startswith("10.1."))
            self.assertNotIn("10.0.4.4", ws["B1"].comment.text)
            self.assertNotIn("10.0.4.4", ws["B1"].hyperlink.target)


if __name__ == "__main__":
    unittest.main()
