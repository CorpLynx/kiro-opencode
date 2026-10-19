"""Discover hosts and open ports on a local network with nmap.

Two nmap runs: a host-discovery sweep (`-sn`) over the targets, then a port
and service scan (`-sV`, top 1000 TCP ports by default) of the hosts that
answered. Doing it in two steps keeps the slow part to live hosts only and
lets the first run's MAC addresses and names survive a second run that
doesn't report them.

The result is one segment per scanned network, each host linked to it, the
default gateway marked as the router, and each host's open services and a
guessed device type in its attributes. `--from-xml` reads saved nmap XML
instead of scanning, so a scan can be redrawn offline (and tested).

Scanning is limited to private, link-local and carrier-grade NAT space
(home and lab networks, Tailscale) unless `--allow-public` is given:
port-scanning someone else's network is easy to do by accident with a typo
and is not something to do unasked. nmap is an external program, not a
Python dependency; a clear error says so when it's missing.
"""

from __future__ import annotations

import ipaddress
import os
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ..hygiene import write_private
from ..model import LINK_SEGMENT, NET_DEVICE, NET_SEGMENT, Topology, node_id

HELP = "Discover hosts and open ports on a local network with nmap, and draw them."

ROLES = ("router", "switch", "firewall", "load-balancer", "wireless", "server", "phone", "unknown")
PRODUCES_TOPOLOGY = True

DEFAULT_TOP_PORTS = 1000
DEFAULT_TIMING = 4
MAX_ADDRESSES = 65536
_CGNAT = ipaddress.ip_network("100.64.0.0/10")

Runner = Callable[[List[str]], str]


@dataclass
class ScanHost:
    address: str
    hostname: str = ""
    mac: str = ""
    mac_vendor: str = ""
    os: str = ""
    ports: List[Dict[str, object]] = field(default_factory=list)


# -- nmap XML -------------------------------------------------------------------

def parse_nmap_xml(text: str) -> Tuple[List[ScanHost], List[str]]:
    """Hosts that were up, and the networks named on nmap's command line
    (from `<nmaprun args=...>`), from nmap's `-oX` output."""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as e:
        raise ValueError(f"not nmap XML output: {e}") from None
    if root.tag != "nmaprun":
        raise ValueError("not nmap XML output: no <nmaprun> element")
    hosts = []
    for h in root.iter("host"):
        status = h.find("status")
        if status is not None and status.get("state") != "up":
            continue
        host = ScanHost("")
        for addr in h.findall("address"):
            kind = addr.get("addrtype")
            if kind in ("ipv4", "ipv6") and not host.address:
                host.address = addr.get("addr", "")
            elif kind == "mac":
                host.mac = addr.get("addr", "").lower()
                host.mac_vendor = addr.get("vendor", "")
        if not host.address:
            continue
        for name in h.iter("hostname"):
            if name.get("name"):
                host.hostname = name.get("name", "")
                break
        for port in h.iter("port"):
            state = port.find("state")
            if state is None or state.get("state") != "open":
                continue
            svc = port.find("service")
            entry: Dict[str, object] = {"port": int(port.get("portid", "0")),
                                        "protocol": port.get("protocol", "tcp")}
            if svc is not None:
                if svc.get("name") and svc.get("name") != "tcpwrapped":
                    entry["service"] = svc.get("name")   # tcpwrapped says nothing about the service
                product = " ".join(x for x in (svc.get("product"), svc.get("version")) if x)
                if product:
                    entry["product"] = product
            host.ports.append(entry)
        best = max(h.iter("osmatch"), key=lambda m: int(m.get("accuracy", "0")), default=None)
        if best is not None:
            host.os = best.get("name", "")
        hosts.append(host)
    return hosts, _networks_in_args(root.get("args", ""))


def _networks_in_args(args: str) -> List[str]:
    out = []
    for token in args.split():
        if "/" in token:
            try:
                out.append(str(ipaddress.ip_network(token, strict=False)))
            except ValueError:
                pass
    return out


def merge_hosts(*scans: List[ScanHost]) -> List[ScanHost]:
    """One host per address; later scans add ports and fill in blanks."""
    by_addr: Dict[str, ScanHost] = {}
    for hosts in scans:
        for h in hosts:
            old = by_addr.get(h.address)
            if old is None:
                by_addr[h.address] = ScanHost(h.address, h.hostname, h.mac, h.mac_vendor, h.os,
                                              list(h.ports))
                continue
            for attr in ("hostname", "mac", "mac_vendor", "os"):
                if not getattr(old, attr):
                    setattr(old, attr, getattr(h, attr))
            seen = {(p["port"], p["protocol"]) for p in old.ports}
            old.ports.extend(p for p in h.ports if (p["port"], p["protocol"]) not in seen)
    for h in by_addr.values():
        h.ports.sort(key=lambda p: (str(p["protocol"]), int(p["port"])))  # type: ignore[arg-type]
    return sorted(by_addr.values(), key=lambda h: ipaddress.ip_address(h.address))


# -- what is it? ----------------------------------------------------------------

_CAMERA_VENDOR = re.compile(r"hikvision|dahua|axis comm|reolink|amcrest|wyze|ring\b", re.I)
_NAS_VENDOR = re.compile(r"synology|qnap|western digital|buffalo|asustor", re.I)
_PRINTER_VENDOR = re.compile(r"brother|canon|epson|lexmark|xerox|kyocera|ricoh", re.I)
_WIFI = re.compile(r"\beero\b|ruckus|aruba networks|\bunifi|\bu6-|\buap\b", re.I)
_MEDIA_VENDOR = re.compile(r"roku|sonos|amazon|google|nvidia|samsung|lg electronics", re.I)


def classify(host: ScanHost, gateway: bool = False) -> Tuple[str, str]:
    """(role, device_type) from open ports, MAC vendor and OS guess.

    `role` is one of ROLES, so the renderers pick the right icon;
    `device_type` is the finer, best-effort guess shown on the label."""
    ports = {int(p["port"]) for p in host.ports}  # type: ignore[arg-type]
    vendor = host.mac_vendor
    if gateway:
        return "router", "gateway"
    if _WIFI.search(f"{vendor} {host.hostname}"):
        return "wireless", "access point"
    if ports & {9100, 515, 631} or _PRINTER_VENDOR.search(vendor):
        return "unknown", "printer"
    if 554 in ports or _CAMERA_VENDOR.search(vendor):
        return "unknown", "camera"
    if ports & {5060, 5061}:
        return "phone", "voip"
    if 8006 in ports or ports & {902, 443} == {902, 443}:
        return "server", "hypervisor"
    if _NAS_VENDOR.search(vendor) or ports & {5000, 5001} and ports & {139, 445, 548, 2049}:
        return "server", "nas"
    if 62078 in ports:
        return "unknown", "mobile"
    if ports & {8008, 8009, 1400, 8060, 7000} or (_MEDIA_VENDOR.search(vendor) and not ports & {22}):
        return "unknown", "media"
    if ports & {22, 21, 25, 53, 80, 110, 143, 443, 445, 2049, 3306, 5432, 6379, 8080, 8443, 9090,
                27017}:
        return "server", "server"
    if ports & {3389, 5900, 135, 139}:
        return "unknown", "workstation"
    return "unknown", ""


# -- topology -------------------------------------------------------------------

def hosts_to_topology(hosts: Sequence[ScanHost], networks: Sequence[str],
                      gateways: Sequence[str] = ()) -> Topology:
    """A segment for each scanned network, a device for each host linked to
    the segment holding its address, the gateway drawn as a router."""
    topo = Topology()
    nets = sorted({ipaddress.ip_network(n, strict=False) for n in networks
                   if ipaddress.ip_network(n, strict=False).num_addresses > 1},
                  key=lambda n: (n.version, n))
    gws = set(gateways)
    names = [_name(h) for h in hosts]
    for net in nets:
        topo.add_node(node_id("net", NET_SEGMENT, str(net)), NET_SEGMENT, str(net), subnet=str(net))
    for h in hosts:
        ip = ipaddress.ip_address(h.address)
        short = _name(h)
        name = short if short and names.count(short) == 1 else h.address
        role, kind = classify(h, h.address in gws)
        home = next((n for n in nets if n.version == ip.version and ip in n), None)
        did = device_id(name)
        topo.add_node(
            did, NET_DEVICE, name,
            mgmt_ip=h.address, hostname=h.hostname or None, mac=h.mac or None,
            vendor=h.mac_vendor or None, os=h.os or None, role=role, device_type=kind or None,
            open_ports=[dict(p) for p in h.ports] or None,
            interfaces=[{"name": "", "addresses": [f"{h.address}/{home.prefixlen}" if home
                                                    else h.address]}],
            sources=["nmap"],
        )
        if home is not None:
            topo.add_link(LINK_SEGMENT, did, node_id("net", NET_SEGMENT, str(home)),
                          address=h.address)
    return topo


_PLACEHOLDER_NAME = re.compile(r"^(unknown|android-?)?[0-9a-f]{12}$")


def _name(host: ScanHost) -> str:
    """The short host name, or "" when there is none worth showing: home
    routers hand out names like "unknown0064af1c4c66" for nameless clients."""
    short = normalize_hostname(host.hostname) if host.hostname else ""
    return "" if _PLACEHOLDER_NAME.match(short) else short


# -- running nmap ---------------------------------------------------------------

def check_targets(targets: Sequence[str], allow_public: bool) -> List[str]:
    """Targets as normalised networks; refuses public space (unless allowed)
    and anything bigger than MAX_ADDRESSES in total, both of which are far
    more likely to be typos than intent on a home or lab network."""
    nets = []
    for t in targets:
        try:
            net = ipaddress.ip_network(t, strict=False)
        except ValueError:
            raise ValueError(f"not an address or network: {t!r} (give e.g. 192.168.1.0/24)") \
                from None
        if not allow_public and not _is_local(net):
            raise ValueError(f"{t} is not private, link-local or CGNAT space; pass "
                             "--allow-public if you really mean to scan it")
        nets.append(net)
    total = sum(n.num_addresses for n in nets)
    if total > MAX_ADDRESSES:
        raise ValueError(f"{total} addresses to scan; split it up (at most {MAX_ADDRESSES})")
    return [str(n) for n in nets]


def _is_local(net) -> bool:
    if net.version == 4 and net.subnet_of(_CGNAT):
        return True
    return net.is_private or net.is_link_local


def default_gateways() -> List[str]:
    """IPv4 default gateways of this machine, best effort: [] if unknown."""
    try:
        with open("/proc/net/route", encoding="ascii") as f:
            out = []
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) > 2 and parts[1] == "00000000":
                    out.append(str(ipaddress.IPv4Address(bytes.fromhex(parts[2])[::-1])))
            return out
    except (OSError, ValueError):
        pass
    try:
        text = subprocess.run(["route", "-n", "get", "default"], capture_output=True, text=True,
                              timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    m = re.search(r"gateway:\s*(\S+)", text)
    return [m.group(1)] if m else []


_ARP_LINE = re.compile(r"\((\d{1,3}(?:\.\d{1,3}){3})\) at ([0-9A-Fa-f]{1,2}(?::[0-9A-Fa-f]{1,2}){5})")


def arp_neighbours() -> Dict[str, str]:
    """address -> MAC from this machine's ARP cache, best effort.

    Without root, nmap finds hosts with TCP pings to a couple of ports, and
    hosts that drop those are missed. The sweep still makes the kernel ARP
    for every address, so the cache afterwards lists every host on the local
    link that answered, firewall or not."""
    out: Dict[str, str] = {}
    try:
        with open("/proc/net/arp", encoding="ascii") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) > 3 and parts[2] != "0x0":
                    out[parts[0]] = parts[3]
    except OSError:
        try:
            text = subprocess.run(["arp", "-an"], capture_output=True, text=True,
                                  timeout=10).stdout
        except (OSError, subprocess.SubprocessError):
            return {}
        for m in _ARP_LINE.finditer(text):
            out[m.group(1)] = m.group(2)
    return {ip: _norm_mac(mac) for ip, mac in out.items()
            if _norm_mac(mac) not in ("ff:ff:ff:ff:ff:ff", "00:00:00:00:00:00")}


def _norm_mac(mac: str) -> str:
    """aa:bb:cc:dd:ee:ff; macOS prints leading zeros dropped (4:c9:...)."""
    return ":".join(part.zfill(2) for part in mac.lower().split(":"))


def mac_vendors(nmap: str) -> Callable[[str], str]:
    """A MAC -> vendor lookup from nmap's own nmap-mac-prefixes file (the
    list nmap uses itself as root), or one that knows nothing."""
    table: Dict[str, str] = {}
    base = os.path.dirname(os.path.dirname(os.path.realpath(nmap)))
    for path in (os.path.join(base, "share", "nmap", "nmap-mac-prefixes"),
                 "/usr/share/nmap/nmap-mac-prefixes", "/usr/local/share/nmap/nmap-mac-prefixes"):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    prefix, _, vendor = line.strip().partition(" ")
                    if prefix and not prefix.startswith("#") and vendor:
                        table[prefix.upper()] = vendor
            break
        except OSError:
            continue

    def lookup(mac: str) -> str:
        digits = mac.replace(":", "").upper()
        for n in range(9, 5, -1):   # newer files have 7- to 9-digit prefixes too
            if digits[:n] in table:
                return table[digits[:n]]
        return ""
    return lookup


def is_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


_NMAP_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin", "/usr/bin")
_PROGRESS = re.compile(r"^(Initiating|Completed|Discovered open port|"
                       r"Nmap scan report for (?!.*\[host down\])|.*Timing: About)")


def find_nmap(name: str) -> Optional[str]:
    """The nmap program: on PATH, or in the usual install folders. Under
    sudo, PATH is cut down and no longer holds Homebrew's folder."""
    found = shutil.which(name)
    if found or os.sep in name:
        return found
    for folder in _NMAP_DIRS:
        path = os.path.join(folder, name)
        if os.access(path, os.X_OK):
            return path
    return None


def nmap_runner(nmap: str, log: Callable[[str], None] = lambda m: None) -> Runner:
    """Run nmap, showing its progress lines through `log` as they appear, and
    return the XML it wrote. The XML goes to a file because nmap's console
    output, with the progress in it, is what we want on the terminal."""
    def run(argv: List[str]) -> str:
        fd, path = tempfile.mkstemp(suffix=".xml")
        os.close(fd)
        try:
            proc = subprocess.Popen([nmap, "-v", "--stats-every", "10s", "-oX", path, *argv],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            tail: List[str] = []
            for line in proc.stdout:  # type: ignore[union-attr]
                line = line.rstrip()
                tail = (tail + [line])[-5:]
                if _PROGRESS.match(line):
                    log(f"nmap: {line}")
            if proc.wait() != 0:
                raise RuntimeError(f"nmap failed (exit {proc.returncode}): "
                                   + " ".join(l for l in tail if l))
            with open(path, encoding="utf-8") as f:
                return f.read()
        finally:
            os.unlink(path)
    return run


def port_options(args) -> List[str]:
    if args.ports:
        return ["-p", args.ports]
    return ["--top-ports", str(args.top_ports)]


def scan(targets: List[str], args, runner: Runner, log: Callable[[str], None],
         neighbours: Optional[Callable[[], Dict[str, str]]] = None,
         vendor: Callable[[str], str] = lambda mac: "") -> \
        Tuple[List[ScanHost], List[Tuple[str, str]]]:
    """Discovery, then a port scan of the live hosts. Returns the hosts and
    the raw XML of each run (name, text), for --save-xml.

    `neighbours` (the ARP cache) adds hosts the sweep missed and MAC
    addresses it couldn't see; `vendor` names the maker of a MAC."""
    common = [f"-T{args.timing}"] + (["-n"] if args.no_dns else [])
    for ex in args.exclude:
        common += ["--exclude", ex]
    log(f"discovering hosts in {', '.join(targets)}")
    xml1 = runner(["-sn", *common, *targets])
    found, _ = parse_nmap_xml(xml1)
    raw = [("discovery.xml", xml1)]
    if neighbours is not None:
        found = _add_neighbours(found, neighbours(), targets, args.exclude, vendor)
    if args.no_ports or not found:
        return merge_hosts(found), raw
    log(f"scanning ports on {len(found)} host(s)")
    argv = ["-Pn", "--open", "-sT", *port_options(args), *common]
    if not args.no_service_detection:
        argv += ["-sV"] + ([] if args.full_probes else ["--version-light"])
    if args.os:
        argv.append("-O")
    xml2 = runner([*argv, *[h.address for h in found]])
    raw.append(("ports.xml", xml2))
    ported, _ = parse_nmap_xml(xml2)
    return merge_hosts(found, ported), raw


def _add_neighbours(found: List[ScanHost], cache: Dict[str, str], targets: Sequence[str],
                    exclude: Sequence[str], vendor: Callable[[str], str]) -> List[ScanHost]:
    nets = [ipaddress.ip_network(t, strict=False) for t in targets]
    skip = []
    for ex in exclude:
        try:
            skip.append(ipaddress.ip_network(ex, strict=False))
        except ValueError:
            pass   # nmap also takes names and ranges; those it excludes itself
    by_addr = {h.address: h for h in found}
    for addr, mac in cache.items():
        ip = ipaddress.ip_address(addr)
        if not any(ip.version == n.version and ip in n for n in nets) \
                or any(ip.version == n.version and ip in n for n in skip):
            continue
        host = by_addr.setdefault(addr, ScanHost(addr))
        host.mac = host.mac or mac
    for host in by_addr.values():
        if host.mac and not host.mac_vendor:
            host.mac_vendor = vendor(host.mac)
    return merge_hosts(list(by_addr.values()))


# -- command --------------------------------------------------------------------

def add_arguments(parser) -> None:
    what = parser.add_argument_group("what to scan")
    what.add_argument("targets", nargs="*", metavar="TARGET",
                      help="networks or addresses to scan, e.g. 192.168.1.0/24")
    what.add_argument("--exclude", action="append", default=[], metavar="ADDR",
                      help="address or network not to scan (repeatable)")
    what.add_argument("--allow-public", action="store_true",
                      help="allow targets outside private, link-local and CGNAT space")
    what.add_argument("--from-xml", action="append", default=[], metavar="FILE",
                      help="read saved nmap -oX output instead of scanning (repeatable); "
                           "TARGETs then only say which networks to draw")

    how = parser.add_argument_group("how to scan")
    ports = how.add_mutually_exclusive_group()
    ports.add_argument("--top-ports", type=int, default=DEFAULT_TOP_PORTS, metavar="N",
                       help=f"scan nmap's N most common TCP ports (default: {DEFAULT_TOP_PORTS})")
    ports.add_argument("--ports", metavar="SPEC", help="ports to scan, nmap syntax: 22,80,1-1024")
    ports.add_argument("--no-ports", action="store_true", help="only discover hosts")
    how.add_argument("--no-service-detection", action="store_true",
                     help="don't probe open ports for service names and versions (-sV); faster")
    how.add_argument("--full-probes", action="store_true",
                     help="service detection tries every probe, not nmap's light set: more "
                          "exact versions, but minutes per host on slow devices like routers")
    how.add_argument("--os", action="store_true", help="guess operating systems (-O; needs root)")
    how.add_argument("--no-dns", action="store_true", help="don't look up host names")
    how.add_argument("--no-arp-cache", action="store_true",
                     help="without root, don't add hosts from this machine's ARP cache that "
                          "the TCP ping sweep missed")
    how.add_argument("--timing", type=int, choices=range(6), default=DEFAULT_TIMING, metavar="0-5",
                     help=f"nmap timing template, slower to faster (default: {DEFAULT_TIMING})")
    how.add_argument("--gateway", action="append", default=[], metavar="ADDR",
                     help="the network's gateway (default: this machine's default gateway)")
    how.add_argument("--nmap", default="nmap", metavar="PATH", help="nmap program to run")
    how.add_argument("--save-xml", dest="save_raw", metavar="DIR",
                     help="also save nmap's XML output to DIR (mode 0600), for --from-xml")


def run(args, runner: Optional[Runner] = None) -> Topology:
    for addr in args.gateway:
        ipaddress.ip_address(addr)
    targets = check_targets(args.targets, args.allow_public)
    warnings: List[str] = []
    if args.from_xml:
        scans, networks = [], list(targets)
        for path in args.from_xml:
            with open(path, encoding="utf-8") as f:
                hosts, nets = parse_nmap_xml(f.read())
            scans.append(hosts)
            if not targets:
                networks += nets
        hosts = merge_hosts(*scans)
    else:
        if not targets:
            raise ValueError("give at least one TARGET to scan, or --from-xml")
        if args.os and not is_root():
            raise ValueError("--os needs root: run with sudo")
        def log(message: str) -> None:
            print(f"topomapper: {message}", file=sys.stderr, flush=True)

        nmap = find_nmap(args.nmap) or args.nmap
        if runner is None:
            if not find_nmap(args.nmap):
                raise RuntimeError("nmap not found: install it (brew install nmap, "
                                   "apt install nmap, pacman -S nmap) or give --nmap PATH")
            runner = nmap_runner(nmap, log)
        root = is_root()
        if not root:
            warnings.append("not running as root: hosts were found with TCP pings"
                            + ("" if args.no_arp_cache else " and this machine's ARP cache")
                            + ", which can miss some; run with sudo for nmap's own ARP discovery")
        use_cache = not root and not args.no_arp_cache
        hosts, raw = scan(targets, args, runner, log,
                          neighbours=arp_neighbours if use_cache else None,
                          vendor=mac_vendors(nmap))
        networks = targets
        if args.save_raw:
            for name, text in raw:
                write_private(os.path.join(args.save_raw, name), text)

    gateways = args.gateway or default_gateways()
    topo = hosts_to_topology(hosts, networks, gateways)
    topo.meta = {"collector": "scan", "networks": sorted(set(networks)), "hosts": len(hosts),
                 "collected_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if not hosts:
        warnings.append("no hosts found")
    elif not args.from_xml and not args.no_ports and not args.no_service_detection:
        ports = [p for h in hosts for p in h.ports]
        if ports and not any(p.get("service") for p in ports):
            warnings.append("service detection named none of the open ports, so only port "
                            "numbers are shown; try --full-probes, or run again")
    for w in warnings:
        topo.warn(w)
    return topo


def normalize_hostname(name: str) -> str:
    """Lower-case and drop the domain, so "nas.example.net" and "nas" are one
    device. An address stays as it is."""
    name = name.strip()
    try:
        ipaddress.ip_address(name)
        return name
    except ValueError:
        pass
    return name.split(".")[0].lower()


def device_id(hostname: str) -> str:
    return node_id("net", NET_DEVICE, normalize_hostname(hostname))
