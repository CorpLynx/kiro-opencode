"""Redaction of pcap / pcapng packet captures (needs scapy).

Every field scapy decodes as an IPv4/IPv6 address is mapped with the same
keyed mapping as text: IP and IPv6 headers (including the quoted headers in
ICMP errors and tunnelled packets), ARP, BOOTP/DHCP fields and options, DNS
A/AAAA answers, ICMP redirect gateways, routing protocol fields, and so on.
Reverse-DNS names (in-addr.arpa / ip6.arpa) and addresses in plain-text
UDP payloads (syslog, SNMP traps sent as text, ...) are rewritten too.

Checksums are recomputed on packets that changed, so the capture stays
valid in Wireshark. Packets that didn't change are written back byte for
byte. TCP payloads are only rewritten when the replacement has the same
length, since anything else would break sequence numbers.
"""

import ipaddress
import logging
import os
import shutil
import tempfile

from .files import SkipFile

ARPA_SUFFIXES = (b".in-addr.arpa", b".ip6.arpa")


def _load_scapy():
    logging.getLogger("scapy").setLevel(logging.ERROR)
    try:
        from scapy.packet import NoPayload, Packet, Padding, Raw
        from scapy.layers.inet import IP, TCP, UDP
        from scapy.layers.inet6 import IPv6
        from scapy.utils import PcapNgWriter, PcapReader, PcapWriter
        from scapy.layers import all as _  # noqa: F401  register every dissector
    except ImportError:
        raise SkipFile("packet captures need scapy (pip install 'ipredact[pcap]')")
    return locals()


class _Rewriter:
    def __init__(self, redactor, reverse, s):
        self.r = redactor
        self.reverse = reverse
        self.s = s
        self.allow_resize = True
        self.skipped = 0  # addresses left alone because resizing was unsafe

    def layers(self, pkt):
        layer = pkt
        while layer is not None and not isinstance(layer, self.s["NoPayload"]):
            yield layer
            layer = layer.payload

    def map_value(self, value):
        """Return (new value, changed, length changed)."""
        if isinstance(value, str):
            try:
                ip = ipaddress.ip_address(value)
            except ValueError:
                return value, False, False
            new = str(self.r.map_addr(ip, None, self.reverse))
            return new, new != value, False
        if isinstance(value, bytes) and value.rstrip(b".").lower().endswith(ARPA_SUFFIXES):
            return self.map_bytes(value, "ascii")
        if isinstance(value, self.s["Packet"]):
            changed, resized = self.rewrite_fields(value)
            return value, changed, resized
        if isinstance(value, (list, tuple)):
            out, changed, resized = [], False, False
            for item in value:
                new, c, z = self.map_value(item)
                out.append(new)
                changed |= c
                resized |= z
            return (type(value)(out) if changed else value), changed, resized
        return value, False, False

    def map_bytes(self, value, encoding):
        new = self.r.redact_text(value.decode(encoding, "surrogateescape"), self.reverse)
        new = new.encode(encoding, "surrogateescape")
        if new == value:
            return value, False, False
        if len(new) != len(value) and not self.allow_resize:
            self.skipped += 1
            return value, False, False
        return new, True, len(new) != len(value)

    def rewrite_fields(self, layer):
        changed = resized = False
        for name, value in list(layer.fields.items()):
            new, c, z = self.map_value(value)
            if c:
                layer.fields[name] = new
                changed = True
                resized |= z
        return changed, resized

    def rewrite(self, pkt, truncated):
        """Rewrite `pkt` in place; True if anything changed."""
        s = self.s
        # Growing or shrinking a payload is only safe where every length that
        # covers it can be recomputed: not inside a TCP stream (sequence
        # numbers) and not in a packet the capture cut short.
        self.allow_resize = not truncated and not pkt.haslayer(s["TCP"])
        changed = resized = False
        for layer in self.layers(pkt):
            c, z = self.rewrite_fields(layer)
            changed |= c
            resized |= z
            if type(layer) is s["Raw"]:  # not Padding: that's link-layer filler
                new, c, z = self.map_bytes(layer.load, "latin-1")
                if c:
                    layer.load = new
                    changed = True
                    resized |= z
        if changed:
            self.fix_up(pkt, resized, truncated)
        return changed

    def fix_up(self, pkt, resized, truncated):
        s = self.s
        for layer in self.layers(pkt):
            quoted = type(layer).__name__.endswith("error") and not isinstance(layer, s["IP"])
            if quoted:
                continue  # headers quoted inside ICMP errors are truncated copies
            if resized:
                for name in ("len", "plen"):
                    if name in layer.fields and isinstance(layer, (s["IP"], s["IPv6"], s["UDP"])):
                        layer.fields[name] = None
            for name in ("chksum", "cksum"):
                if name not in layer.fields:
                    continue
                if truncated and not isinstance(layer, s["IP"]):
                    continue  # payload incomplete: can't recompute; only IP header sums
                if isinstance(layer, s["UDP"]) and layer.fields[name] == 0:
                    continue  # 0 means "no checksum" for UDP over IPv4
                layer.fields[name] = None
        pkt.clear_cache()


def redact_capture_file(redactor, infile, outfile, reverse, notes):
    s = _load_scapy()
    head = infile.read(4)
    infile.seek(0)
    pcapng = head == b"\x0a\x0d\x0d\x0a"
    reader = s["PcapReader"](infile)
    tmp_path = None
    if pcapng:
        # PcapNgWriter only takes a filename: write to a private temp file
        # and stream it into the (atomic) output afterwards.
        fd, tmp_path = tempfile.mkstemp(prefix="ipredact-", suffix=".pcapng")
        os.close(fd)
        writer = s["PcapNgWriter"](tmp_path)
    else:
        writer = s["PcapWriter"](outfile, linktype=getattr(reader, "linktype", None),
                                 nano=getattr(reader, "nano", False), sync=False)
    rw = _Rewriter(redactor, reverse, s)
    total = changed = 0
    try:
        for pkt in reader:
            total += 1
            wirelen = getattr(pkt, "wirelen", None)
            truncated = wirelen is not None and wirelen > len(bytes(pkt))
            if rw.rewrite(pkt, truncated):
                changed += 1
            comment = getattr(pkt, "comment", None)
            if comment:
                text = comment.decode("utf-8", "surrogateescape") if isinstance(comment, bytes) else comment
                new = redactor.redact_text(text, reverse)
                if new != text:
                    pkt.comment = new.encode("utf-8", "surrogateescape") if isinstance(comment, bytes) else new
            writer.write(pkt)
        if tmp_path:
            writer.close()
        else:
            writer.flush()
        if tmp_path:
            with open(tmp_path, "rb") as f:
                shutil.copyfileobj(f, outfile)
    finally:
        if tmp_path:
            os.unlink(tmp_path)
    if total == 0:
        raise SkipFile("capture contains no packets scapy could read")
    notes.append(f"info: {changed} of {total} packet(s) rewritten")
    if rw.skipped:
        notes.append(f"warning: {rw.skipped} address(es) in TCP or truncated payloads were left "
                     f"as-is because rewriting them would change the payload length")
    if pcapng:
        notes.append("info: pcapng metadata blocks (interface descriptions, name resolution) are not carried over")
