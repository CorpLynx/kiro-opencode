import logging
import os
import struct
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from ipredact import Redactor  # noqa: E402

logging.getLogger("scapy").setLevel(logging.ERROR)
try:
    from scapy.all import (ARP, BOOTP, DHCP, DNS, DNSQR, DNSRR, ICMP, IP, TCP, UDP, Ether,
                           IPv6, PcapNgWriter, Raw, rdpcap, wrpcap)
except ImportError:  # pragma: no cover
    IP = None

KEY = "test-key-0123456789"


def run_cli(*args, check=True):
    return subprocess.run([sys.executable, "-m", "ipredact", *args], capture_output=True,
                          check=check, cwd=ROOT)


def sample_packets():
    eth = Ether(src="00:11:22:33:44:55", dst="66:77:88:99:aa:bb")
    return [
        eth / IP(src="10.1.2.3", dst="10.1.9.9") / TCP(sport=40000, dport=443, flags="S"),
        eth / IP(src="10.1.2.3", dst="172.16.5.5") / UDP(sport=5353, dport=53)
        / DNS(qr=1, qd=DNSQR(qname="db.corp."),
              an=DNSRR(rrname="db.corp.", type="A", rdata="10.1.7.7")),
        eth / IP(src="10.1.2.3", dst="172.16.5.5") / UDP(sport=5354, dport=53)
        / DNS(qd=DNSQR(qname="3.2.1.10.in-addr.arpa.", qtype="PTR")),
        eth / ARP(op=1, psrc="10.1.2.3", pdst="10.1.2.254"),
        eth / IP(src="10.1.2.254", dst="255.255.255.255") / UDP(sport=67, dport=68)
        / BOOTP(yiaddr="10.1.2.50", siaddr="10.1.2.254")
        / DHCP(options=[("message-type", "offer"), ("router", "10.1.2.1"),
                        ("name_server", "10.1.7.7", "10.1.7.8"), "end"]),
        eth / IP(src="10.1.2.254", dst="10.1.2.3") / ICMP(type=3, code=3)
        / IP(src="10.1.2.3", dst="10.1.9.9") / UDP(sport=1, dport=2),
        eth / IP(src="10.1.2.3", dst="10.1.5.5") / UDP(sport=514, dport=514)
        / Raw(b"<13>sshd: accepted from 10.1.2.3 port 22"),
        eth / IP(src="10.1.2.3", dst="10.1.5.5") / TCP(sport=1, dport=80, flags="PA")
        / Raw(b"GET / HTTP/1.1\r\nHost: 10.1.5.5\r\n\r\n"),
        eth / IPv6(src="2001:db8:abcd:12::1", dst="2001:db8:abcd:99::1") / UDP(sport=1, dport=2),
        eth / IP(src="8.8.8.8", dst="127.0.0.1") / ICMP(),  # 127.0.0.1 excluded
    ]


def checksums_valid(pkt):
    """Rebuilding with every checksum cleared gives back the same bytes."""
    fresh = pkt.__class__(bytes(pkt))
    for layer in fresh.iterpayloads():
        if type(layer).__name__.endswith("error") and not isinstance(layer, IP):
            continue
        for name in ("chksum", "cksum"):
            if name in layer.fields:
                layer.fields[name] = None
    fresh.clear_cache()
    return bytes(fresh.__class__(bytes(fresh))) == bytes(pkt)


@unittest.skipIf(IP is None, "needs scapy")
class CaptureTest(unittest.TestCase):
    def setUp(self):
        self.r = Redactor(KEY)
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def path(self, name):
        return os.path.join(self.d, name)

    def write_pcap(self, name, pkts):
        pkts = [p.__class__(bytes(p)) for p in pkts]  # concrete bytes with real checksums
        for i, p in enumerate(pkts):
            p.time = 1700000000 + i
        wrpcap(self.path(name), pkts)
        return pkts

    def test_headers_protocols_and_checksums(self):
        orig = self.write_pcap("in.pcap", sample_packets())
        res = run_cli(self.path("in.pcap"), "-o", self.path("out.pcap"), "-k", KEY, "-v")
        out = rdpcap(self.path("out.pcap"))
        self.assertEqual(len(out), len(orig))
        m = self.r.redact_text
        a, b = out[0], out[1]
        self.assertEqual((a[IP].src, a[IP].dst), (m("10.1.2.3"), m("10.1.9.9")))
        self.assertEqual(b[DNS].an[0].rdata, m("10.1.7.7"))
        self.assertEqual(b[IP].dst, m("172.16.5.5"))
        self.assertEqual(out[2][DNSQR].qname.decode(), m("3.2.1.10.in-addr.arpa."))
        self.assertEqual((out[3][ARP].psrc, out[3][ARP].pdst), (m("10.1.2.3"), m("10.1.2.254")))
        self.assertEqual(out[4][IP].dst, "255.255.255.255")
        self.assertEqual(out[4][BOOTP].yiaddr, m("10.1.2.50"))
        opts = dict((o[0], o[1:]) for o in out[4][DHCP].options if isinstance(o, tuple))
        self.assertEqual(opts["router"], (m("10.1.2.1"),))
        self.assertEqual(opts["name_server"], (m("10.1.7.7"), m("10.1.7.8")))
        inner = out[5][ICMP].payload
        self.assertEqual((inner.src, inner.dst), (m("10.1.2.3"), m("10.1.9.9")))
        self.assertEqual(out[6][Raw].load, b"<13>sshd: accepted from " + m("10.1.2.3").encode() + b" port 22")
        self.assertEqual(out[8][IPv6].src, m("2001:db8:abcd:12::1"))
        for p in out:
            self.assertTrue(checksums_valid(p), p.summary())
        self.assertEqual(bytes(out[9][IP].dst.encode()), b"127.0.0.1")
        self.assertIn(b"packet(s) rewritten", res.stderr)

        # The TCP payload address only changes if its length is unchanged.
        if len(m("10.1.5.5")) != len("10.1.5.5"):
            self.assertIn(b"10.1.5.5", bytes(out[7][Raw].load))
            self.assertIn(b"left as-is", res.stderr)
        self.assertEqual(out[7][IP].dst, m("10.1.5.5"))

        # Timestamps carried over.
        self.assertEqual([int(p.time) for p in out], [int(p.time) for p in orig])

    def test_round_trip_is_byte_identical(self):
        orig = self.write_pcap("in.pcap", sample_packets())
        run_cli(self.path("in.pcap"), "-o", self.path("red.pcap"), "-k", KEY)
        run_cli(self.path("red.pcap"), "-o", self.path("back.pcap"), "-k", KEY, "-r")
        back = rdpcap(self.path("back.pcap"))
        for o, b in zip(orig, back):
            self.assertEqual(bytes(o), bytes(b), o.summary())

    def test_pcapng_and_comments(self):
        pkts = [p.__class__(bytes(p)) for p in sample_packets()[:2]]
        pkts[0].comment = b"suspicious host 10.1.2.3"
        w = PcapNgWriter(self.path("in.pcapng"))
        for p in pkts:
            w.write(p)
        w.close()
        run_cli(self.path("in.pcapng"), "-o", self.path("out.pcapng"), "-k", KEY)
        with open(self.path("out.pcapng"), "rb") as f:
            self.assertEqual(f.read(4), b"\x0a\x0d\x0d\x0a")
        out = rdpcap(self.path("out.pcapng"))
        self.assertEqual(out[0][IP].src, self.r.redact_text("10.1.2.3"))
        self.assertEqual(out[0].comment, b"suspicious host " + self.r.redact_text("10.1.2.3").encode())

    def test_truncated_packets(self):
        pkt = Ether() / IP(src="10.1.2.3", dst="10.1.9.9") / UDP(sport=1, dport=514) / Raw(b"x" * 200 + b" 10.1.2.3")
        full = bytes(pkt)
        snap = full[:60]
        # Hand-built pcap: caplen 60, wirelen = the full packet.
        header = struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1)
        record = struct.pack("<IIII", 1700000000, 0, len(snap), len(full)) + snap
        with open(self.path("in.pcap"), "wb") as f:
            f.write(header + record)
        res = run_cli(self.path("in.pcap"), "-o", self.path("out.pcap"), "-k", KEY)
        out = rdpcap(self.path("out.pcap"))[0]
        self.assertEqual(out[IP].src, self.r.redact_text("10.1.2.3"))
        self.assertEqual(out[IP].len, len(full) - 14)  # still the on-the-wire length
        self.assertEqual(out.wirelen, len(full))
        fresh = IP(bytes(out[IP]))
        fresh.chksum = None
        self.assertEqual(IP(bytes(fresh)).chksum, out[IP].chksum)  # header checksum fixed
        self.assertEqual(res.returncode, 0)

    def test_unchanged_packets_are_byte_identical(self):
        pkts = self.write_pcap("in.pcap", [Ether() / IP(src="127.0.0.1", dst="127.0.0.1") / UDP(chksum=0xBEEF)])
        run_cli(self.path("in.pcap"), "-o", self.path("out.pcap"), "-k", KEY)
        self.assertEqual(bytes(rdpcap(self.path("out.pcap"))[0]), bytes(pkts[0]))


if __name__ == "__main__":
    unittest.main()
