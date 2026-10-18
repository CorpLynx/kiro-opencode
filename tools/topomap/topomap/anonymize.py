"""Make a snapshot safe to share or commit.

A real run is full of things that identify an estate: account and
organization IDs, resource IDs, names, tags, e-mail addresses and
addresses. This replaces them with consistent fakes, so the same real value
always becomes the same fake and the picture keeps its shape:

* **AWS IDs** keep their prefix and length (`vpc-0a1b...` becomes
  `vpc-0000000000000001`); account IDs become `1000000000NN`.
* **Names** (account, OU, policy, device and site names, Name tags and
  other tag values) become `account-1`, `ou-2`, `device-3`, ...
* **Free text** (descriptions, serial numbers, source file names) is
  replaced whole.
* **Addresses** go through `ipredact` when it's installed, which keeps
  prefixes and containment true. `--ips public` is the dependency-free
  fallback: it moves only public addresses into the documentation ranges and
  leaves private ones alone, and `--ips keep` leaves them all.

Nothing maps back: no key or table is written, so the fakes can't be undone.
After rewriting, the output is searched for every original ID; any that
survived is a warning (the value itself is never printed).
"""

from __future__ import annotations

import importlib.util
import ipaddress
import json
import re
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional, Set, Tuple

from .merge import read_topology
from .model import Topology

HELP = ("Replace account IDs, resource IDs, names, tags and addresses with consistent fakes, "
        "so a snapshot can be shared or committed.")
PRODUCES_TOPOLOGY = True
SENSITIVE = False

IPS_MODES = ("ipredact", "public", "keep")

_OU = re.compile(r"(?<![\w-])ou-([0-9a-z]{4,32})-([0-9a-z]{8,32})(?![\w-])")
_SHORT = {  # Organizations IDs: root, organization, policy
    "r": re.compile(r"(?<![\w-])r-([0-9a-z]{4,32})(?![\w-])"),
    "o": re.compile(r"(?<![\w-])o-([0-9a-z]{10,32})(?![\w-])"),
    "p": re.compile(r"(?<![\w-])p-([0-9a-z]{8,32})(?![\w-])"),
}
_RESOURCE = re.compile(r"(?<![\w-])((?:[a-z]+-)*[a-z]+)-([0-9a-f]{17}|[0-9a-f]{8})(?![\w-])")
_ACCOUNT = re.compile(r"(?<!\d)\d{12}(?!\d)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_MAC = re.compile(r"(?<![0-9A-Fa-f:.-])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:.-])"
                  r"|(?<![0-9A-Fa-f.])(?:[0-9A-Fa-f]{4}\.){2}[0-9A-Fa-f]{4}(?![0-9A-Fa-f.])")
_IPV4 = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?!\d|\.\d)")
_IPV6 = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:])")
_LABEL = re.compile(r"^(.*\S) \(([^()]+)\)$")

# Kinds whose label is the name a person gave the thing.
_NAMED_KINDS = {"account": "account", "ou": "ou", "org-policy": "policy", "device": "device",
                "site": "site"}
_GENERIC = {"root", "main", "default", "internet"}
_FREE_TEXT = {"description": "description", "comment": "comment", "serial": "serial",
              "sources": "source"}
_MIN_NAME = 3
_DOC_NETS = (ipaddress.ip_network("192.0.2.0/24"), ipaddress.ip_network("198.51.100.0/24"),
             ipaddress.ip_network("203.0.113.0/24"))


class _Mapper:
    def __init__(self, ips: str):
        self.ips = ips
        self.names: Dict[str, str] = {}        # lower-case original -> fake
        self.counters: Dict[str, int] = {}
        self.ids: Dict[Tuple[str, str], str] = {}
        self.originals: Set[str] = set()       # IDs and e-mail addresses, for the leak check
        self.free: Dict[Tuple[str, str], str] = {}
        self.ipv4: Dict[str, str] = {}
        self.ipv6: Dict[str, str] = {}
        self._rx: Optional["re.Pattern[str]"] = None

    def _next(self, scope: str) -> int:
        self.counters[scope] = self.counters.get(scope, 0) + 1
        return self.counters[scope]

    # -- names ------------------------------------------------------------------

    def add_name(self, name: Any, prefix: str, always: bool = False) -> Optional[str]:
        """Register `name` for replacement, returning its fake. Short or
        generic names are only registered when `always`, and then only
        replaced where they stand alone (a tag value), never inside text."""
        if not isinstance(name, str) or not name.strip():
            return None
        name = name.strip()
        key = name.lower()
        if key in self.names:
            return self.names[key]
        if not always and (len(name) < _MIN_NAME or key in _GENERIC or self._is_id(name)):
            return None
        self.names[key] = f"{prefix}-{self._next('name:' + prefix)}"
        self._rx = None
        return self.names[key]

    def _is_id(self, text: str) -> bool:
        return any(rx.fullmatch(text) for rx in (_OU, _RESOURCE, _ACCOUNT, *_SHORT.values()))

    def _name_rx(self) -> Optional["re.Pattern[str]"]:
        if self._rx is None:
            usable = [n for n in self.names if len(n) >= _MIN_NAME and n not in _GENERIC]
            if not usable:
                return None
            alt = "|".join(re.escape(n) for n in sorted(usable, key=lambda n: (-len(n), n)))
            self._rx = re.compile(rf"(?<![A-Za-z0-9])(?:{alt})(?![A-Za-z0-9])", re.I)
        return self._rx

    # -- identifiers ------------------------------------------------------------

    def _fake_id(self, scope: str, original: str, make) -> str:
        key = (scope, original)
        if key not in self.ids:
            self.originals.add(original)
            self.ids[key] = make(self._next("id:" + scope))
        return self.ids[key]

    def identifiers(self, text: str) -> str:
        def ou(m):
            a, b = m.group(1), m.group(2)
            return self._fake_id("ou", m.group(0), lambda n: f"ou-{n:0{len(a)}x}-{n:0{len(b)}x}")

        text = _OU.sub(ou, text)
        for letter, rx in _SHORT.items():
            text = rx.sub(lambda m, letter=letter: self._fake_id(
                letter, m.group(0), lambda n: f"{letter}-{n:0{len(m.group(1))}x}"), text)
        text = _RESOURCE.sub(lambda m: self._fake_id(
            m.group(1), m.group(0), lambda n: f"{m.group(1)}-{n:0{len(m.group(2))}x}"), text)
        text = _ACCOUNT.sub(lambda m: self._fake_id(
            "account", m.group(0), lambda n: f"{100000000000 + n:012d}"), text)
        text = _EMAIL.sub(lambda m: self._fake_id(
            "email", m.group(0), lambda n: f"user-{n}@example.com"), text)
        return _MAC.sub(lambda m: self._fake_id(
            "mac", m.group(0), lambda n: f"02:00:00:00:{n >> 8 & 255:02x}:{n & 255:02x}"), text)

    # -- addresses --------------------------------------------------------------

    def public_addresses(self, text: str) -> str:
        def v4(m):
            try:
                ip = ipaddress.IPv4Address(m.group(0))
            except ValueError:
                return m.group(0)
            if not ip.is_global:
                return m.group(0)
            if m.group(0) not in self.ipv4:
                n = len(self.ipv4)
                if n >= 254 * len(_DOC_NETS):
                    raise ValueError("more public IPv4 addresses than the documentation ranges "
                                     "hold; use --ips ipredact or --ips keep")
                net = _DOC_NETS[n // 254]
                self.ipv4[m.group(0)] = str(net.network_address + n % 254 + 1)
            return self.ipv4[m.group(0)]

        def v6(m):
            try:
                ip = ipaddress.IPv6Address(m.group(0))
            except ValueError:
                return m.group(0)
            if not ip.is_global:
                return m.group(0)
            key = ip.compressed
            if key not in self.ipv6:
                self.ipv6[key] = f"2001:db8::{len(self.ipv6) + 1:x}"
            return self.ipv6[key]

        return _IPV6.sub(v6, _IPV4.sub(v4, text))

    # -- text and structure -------------------------------------------------------

    def text(self, value: str) -> str:
        rx = self._name_rx()
        if rx is not None:
            value = rx.sub(lambda m: self.names[m.group(0).lower()], value)
        value = self.identifiers(value)
        return self.public_addresses(value) if self.ips == "public" else value

    def free_text(self, key: str, value: str) -> str:
        pair = (key, value)
        if pair not in self.free:
            self.free[pair] = f"{_FREE_TEXT[key]}-{self._next('free:' + key)}"
        return self.free[pair]

    def walk(self, obj: Any, key: str = "") -> Any:
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                new_key = self.text(k) if isinstance(k, str) else k
                if k == "tags" and isinstance(v, dict):
                    out[new_key] = {self.text(tk): self._tag_value(tv) for tk, tv in v.items()}
                else:
                    out[new_key] = self.walk(v, k)
            return out
        if isinstance(obj, list):
            return [self.walk(v, key) for v in obj]
        if isinstance(obj, str):
            if key in _FREE_TEXT and obj:
                return self.free_text(key, obj)
            return self.text(obj)
        return obj

    def _tag_value(self, value: Any) -> Any:
        if not isinstance(value, str) or not value:
            return value
        return self.add_name(value, "tag", always=True)


def _harvest(topo: Topology, mapper: _Mapper, keep_names: bool) -> None:
    """Find every name worth replacing before any text is rewritten, so a
    name is replaced the same way wherever it turns up."""
    if keep_names:
        return
    nodes = sorted(topo.nodes.values(), key=lambda n: n.id)
    for node in nodes:
        prefix = _NAMED_KINDS.get(node.kind, "resource")
        m = _LABEL.match(node.label or "")
        if m and m.group(2) in node.id:
            mapper.add_name(m.group(1), prefix)
        elif node.kind in _NAMED_KINDS and node.label and node.label != node.id:
            mapper.add_name(node.label, prefix)
        if node.kind in ("device", "site"):
            mapper.add_name(node.id.rsplit(":", 1)[-1], prefix)
        for attr in ("name", "hostname", "device_name"):
            mapper.add_name(node.attrs.get(attr), prefix)
        policies = node.attrs.get("policies")
        if isinstance(policies, dict):
            for names in policies.values():
                for name in names if isinstance(names, list) else []:
                    mapper.add_name(name, "policy")
    for node in nodes:
        tags = node.attrs.get("tags")
        for value in tags.values() if isinstance(tags, dict) else ():
            mapper.add_name(value, "tag", always=True)


def anonymize(topo: Topology, ips: str = "public", keep_names: bool = False,
              ip_key_file: Optional[str] = None) -> Topology:
    """An anonymized copy of `topo`; the input is not changed."""
    if ips not in IPS_MODES:
        raise ValueError(f"--ips must be one of {', '.join(IPS_MODES)}, not {ips!r}")
    mapper = _Mapper(ips)
    _harvest(topo, mapper, keep_names)
    data = mapper.walk(json.loads(topo.dumps()))
    data.setdefault("meta", {})["anonymized"] = True
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    if ips == "ipredact":
        text = _ipredact(text, ip_key_file)
    out = Topology.loads(text)
    leaked = sum(1 for original in mapper.originals if original in text)
    if leaked:
        out.warn(f"anonymize: {leaked} original identifier(s) are still present in the output")
    for problem in out.validate():
        out.warn(f"anonymize: {problem}")
    return out


def _ipredact_command() -> List[str]:
    found = shutil.which("ipredact")
    if found:
        return [found]
    if importlib.util.find_spec("ipredact") is not None:
        return [sys.executable, "-m", "ipredact"]
    raise RuntimeError("--ips ipredact needs the ipredact tool: pip install ./tools/ip-redactor "
                       "(or use --ips public, which only rewrites public addresses, or --ips keep)")


def _ipredact(text: str, key_file: Optional[str]) -> str:
    command = _ipredact_command()
    if key_file:
        command += ["--key-file", key_file]
    done = subprocess.run(command, input=text, capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"ipredact failed: {done.stderr.strip() or done.returncode}")
    return done.stdout


def add_arguments(parser) -> None:
    parser.add_argument("input", nargs="?", default="-", metavar="TOPOLOGY",
                        help="topology JSON to anonymize (default: stdin)")
    parser.add_argument("--ips", choices=IPS_MODES, default="ipredact",
                        help="addresses: ipredact (structure-preserving, needs the ipredact tool), "
                             "public (only public addresses, into documentation ranges) or keep "
                             "(default: ipredact)")
    parser.add_argument("--ip-key-file", metavar="FILE",
                        help="ipredact key file, to get the same addresses on every run")
    parser.add_argument("--keep-names", action="store_true",
                        help="leave names and tag values alone; IDs and addresses are still replaced")


def run(args) -> Topology:
    if args.ip_key_file and args.ips != "ipredact":
        raise ValueError("--ip-key-file only applies to --ips ipredact")
    return anonymize(read_topology(args.input), ips=args.ips, keep_names=args.keep_names,
                     ip_key_file=args.ip_key_file)
