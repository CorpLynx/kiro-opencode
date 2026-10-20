"""Make a snapshot safe to share or commit.

A real run is full of things that identify an estate: account and
organization IDs, resource IDs, names, tags, e-mail addresses and
addresses. This replaces them with consistent fakes, so the same real value
always becomes the same fake and the picture keeps its shape:

* **AWS IDs** keep their prefix and length (`vpc-0a1b...` becomes
  `vpc-0000000000000001`); account IDs become `1000000000NN`.
* **Names** (account, OU, policy, role and user names, Name tags and
  other tag values) become `account-1`, `ou-2`, `role-3`, ...
* **Free text** (descriptions, comments, serial numbers) is
  replaced whole.
* **Addresses** go through `ipredact` when it's installed, which keeps
  prefixes and containment true. `--ips public` is the dependency-free
  fallback: it moves only public addresses into the documentation ranges and
  leaves private ones alone, and `--ips keep` leaves them all.

Nothing maps back: no key or table is written, so the fakes can't be undone.
`--strict` also drops every attribute that isn't on a short list of known,
non-free-text ones, for when an unfamiliar field might hold something
personal. After rewriting, the output is searched for every original ID,
name and public address; any that survived is a warning (the value itself
is never printed).
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

from .cli import OUTPUT_HELP
from .merge import read_topology
from .model import NODE_KINDS, Topology

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
# IAM unique IDs (AROA... for a role, AIDA... for a user), which AWS Config
# uses as their resource IDs, and UUIDs (KMS keys, stacks, ...).
_UNIQUE = re.compile(r"(?<![A-Z0-9])(A(?:RO|ID|GP|NP|NV|KI|SI|PK|IP|NA)A)([A-Z0-9]{12,28})(?![A-Za-z0-9])")
_UUID = re.compile(r"(?<![0-9A-Za-z])[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{12}(?![0-9A-Za-z])")
_OIDC = re.compile(r"(?<=/id/)[0-9A-Fa-f]{32}(?![0-9A-Za-z])")  # an EKS cluster's OIDC issuer ID
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
_MAC = re.compile(r"(?<![0-9A-Fa-f:.-])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:.-])"
                  r"|(?<![0-9A-Fa-f.])(?:[0-9A-Fa-f]{4}\.){2}[0-9A-Fa-f]{4}(?![0-9A-Fa-f.])")
_IPV4 = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?!\d|\.\d)")
_IPV6 = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){2,7}[0-9A-Fa-f]{0,4}(?![\w:])")
_LABEL = re.compile(r"^(.*\S) \(([^()]+)\)$")

# Kinds whose label is the name a person gave the thing.
_NAMED_KINDS = {"account": "account", "ou": "ou", "org-policy": "policy",
                "iam-role": "role", "iam-user": "user", "eks-cluster": "cluster",
                "aws-resource": "resource"}
_GENERIC = {"root", "main", "default", "internet"}
_FREE_TEXT = {"description": "description", "comment": "comment", "serial": "serial",
              "sources": "source"}
_MIN_NAME = 3
IPREDACT_TIMEOUT = 300

# Attributes --strict keeps: everything DESIGN.md documents that isn't
# free text. Nested keys (interfaces, routes, tunnels) are listed too.
_SAFE_KEYS = frozenset("""
    account_id management_account_id org_id root_id ou_id policy_id feature_set arn path name
    policy_types policies type aws_managed status joined joined_method management collected empty
    delegated_admin_for region az az_id vpc_id subnet_id gateway_id nat_gateway_id endpoint_id
    tgw_id vgw_id cgw_id connection_id dxgw_id instance_id cidr cidrs ipv6_cidrs is_default public
    routes destination target route_tables id attachment peering_id attachment_id public_ips
    private_ips connectivity service endpoint_type asn ip location bandwidth state private_ips
    public_ip instance_type source_port target_port vpn_id routing
    tunnels outside_ip inside_cidr vif_id vif_type vlan customer_address amazon_address amazon_asn
    association_id allowed_prefixes vif_attachments resource_type external collapsed
    collapsed_links device_name
    model_id model_name provider input_modalities output_modalities inference_types streaming
    lifecycle source models_shown models_offered models_in_use bedrock_models_shown
    bedrock_models_offered evidence invocable
    base_model version platform_version endpoint_public_access endpoint_private_access created
    summary aws_owned admin_access trusts_everyone trusts_own_account mfa_enabled active_access_keys nodegroups
    instance_types capacity_type ami_type desired min max
    resource_id found_by relationships relation inventory_sources resources summarized subnet_ids
""".split())
_DOC_NETS = (ipaddress.ip_network("192.0.2.0/24"), ipaddress.ip_network("198.51.100.0/24"),
             ipaddress.ip_network("203.0.113.0/24"))


class _Mapper:
    def __init__(self, ips: str):
        self.ips = ips
        self.names: Dict[str, str] = {}        # lower-case original -> fake
        self.pending: Dict[str, Tuple[str, str, bool]] = {}
        self.taken: Set[str] = set()           # every original, so no fake can equal one
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

    def want(self, name: Any, prefix: str, always: bool = False) -> None:
        """Note a name to replace. Short or generic names are only kept when
        `always`, and then only replaced where they stand alone (a tag
        value), never inside other text."""
        if not isinstance(name, str) or not name.strip():
            return
        name = name.strip()
        key = name.lower()
        if key in self.pending:
            return
        if not always and (len(name) < _MIN_NAME or key in _GENERIC or self._is_id(name)):
            return
        self.pending[key] = (name, prefix, always)

    def assign_names(self) -> None:
        """Give each wanted name its fake, in a stable order, skipping any
        fake that equals a real value (which would merge two things)."""
        self.taken |= set(self.pending)
        for key, (_, prefix, _) in sorted(self.pending.items(), key=lambda kv: kv[1][1:] + (kv[0],)):
            fake = f"{prefix}-{self._next('name:' + prefix)}"
            while fake.lower() in self.taken:
                fake = f"{prefix}-{self._next('name:' + prefix)}"
            self.names[key] = fake
        self._rx = None

    def known_name(self, name: str) -> Optional[str]:
        return self.names.get(name.strip().lower())

    def _is_id(self, text: str) -> bool:
        return any(rx.fullmatch(text) for rx in (_OU, _RESOURCE, _ACCOUNT, _UNIQUE, _UUID,
                                                 *_SHORT.values()))

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
            fake = make(self._next("id:" + scope))
            while fake in self.taken:
                fake = make(self._next("id:" + scope))
            self.ids[key] = fake
        return self.ids[key]

    def scan(self, text: str) -> None:
        """Note every identifier already in the input, so a fake never
        lands on a real one."""
        for rx in (_OU, _RESOURCE, _OIDC, _ACCOUNT, _EMAIL, _UNIQUE, _UUID, *_SHORT.values()):
            self.taken.update(m.group(0) for m in rx.finditer(text))

    def identifiers(self, text: str) -> str:
        def ou(m):
            a, b = m.group(1), m.group(2)
            return self._fake_id("ou", m.group(0), lambda n: f"ou-{n:0{len(a)}x}-{n:0{len(b)}x}")

        text = _UUID.sub(lambda m: self._fake_id(
            "uuid", m.group(0), lambda n: f"00000000-0000-4000-8000-fa4e{n:08x}"), text)
        text = _UNIQUE.sub(lambda m: self._fake_id(
            "unique", m.group(0), lambda n: f"{m.group(1)}{n:0{len(m.group(2))}d}"), text)
        text = _OU.sub(ou, text)
        for letter, rx in _SHORT.items():
            text = rx.sub(lambda m, letter=letter: self._fake_id(
                letter, m.group(0), lambda n: f"{letter}-{n:0{len(m.group(1))}x}"), text)
        text = _RESOURCE.sub(lambda m: self._fake_id(
            m.group(1), m.group(0), lambda n: f"{m.group(1)}-{n:0{len(m.group(2))}x}"), text)
        text = _OIDC.sub(lambda m: self._fake_id(
            "oidc", m.group(0), lambda n: f"{n:032X}"), text)
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
        return self.known_name(value) or value


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
            mapper.want(m.group(1), prefix)
        elif node.kind in _NAMED_KINDS and node.label and node.label != node.id:
            mapper.want(node.label, prefix)
        for attr in ("name", "device_name", "cluster_name", "role_name", "user_name"):
            mapper.want(node.attrs.get(attr), prefix)
        if node.kind == "bedrock-model" and node.attrs.get("source") in ("custom", "imported"):
            mapper.want(node.label, "model")
        policies = node.attrs.get("policies")
        if isinstance(policies, dict):
            for names in policies.values():
                for name in names if isinstance(names, list) else []:
                    mapper.want(name, "policy")
        _harvest_lists(node.attrs, mapper)
    links = sorted(topo.links.values(), key=lambda l: l.id)
    for link in links:
        m = _LABEL.match(link.label or "")
        if m:
            mapper.want(m.group(1), "resource")
    for item in [*nodes, *links]:
        tags = item.attrs.get("tags")
        for value in tags.values() if isinstance(tags, dict) else ():
            mapper.want(value, "tag", always=True)
    mapper.assign_names()


def _strict(value: Any) -> Any:
    """`value` with every dict key outside _SAFE_KEYS removed, at any depth."""
    if isinstance(value, dict):
        return {k: _strict(v) for k, v in value.items() if k in _SAFE_KEYS}
    if isinstance(value, list):
        return [_strict(v) for v in value]
    return value


def _bare(end: str) -> str:
    return end.split("[", 1)[0]


def _canonical_links(topo: Topology) -> None:
    """Renaming can reorder two ends ("vpc-10" sorts before "vpc-2"),
    but an undirected link must keep source < target, with ports and the ID
    following, or `diff` and `merge` will see a different link."""
    for link in list(topo.links.values()):
        if link.directed or link.source <= link.target:
            continue
        parts = link.id.split("|")
        in_id = len(parts) >= 3 and (_bare(parts[1]), _bare(parts[2])) == (link.source, link.target)
        link.source, link.target = link.target, link.source
        ports = link.attrs
        if "source_port" in ports or "target_port" in ports:
            a, b = ports.pop("source_port", None), ports.pop("target_port", None)
            if b is not None:
                ports["source_port"] = b
            if a is not None:
                ports["target_port"] = a
        if in_id:
            parts[1], parts[2] = parts[2], parts[1]
            del topo.links[link.id]
            link.id = "|".join(parts)
            topo.links[link.id] = link


_STRUCTURAL = frozenset(NODE_KINDS) | {"true", "false", "null", "none"}


def _survivors(mapper: _Mapper, text: str) -> Dict[str, int]:
    """How many originals of each sort are still in `text`."""
    found = {"identifier": sum(1 for o in mapper.originals if o in text),
             "public address": sum(1 for o in list(mapper.ipv4) + list(mapper.ipv6)
                                   if re.search(rf"(?<![\w.:]){re.escape(o)}(?![\w:]|\.\d)", text))}
    rest = text
    for fake in sorted({*mapper.names.values(), *mapper.ids.values(), *mapper.free.values()},
                       key=len, reverse=True):
        rest = rest.replace(fake, "")
    rest = re.sub(r'"[^"\n]*":', "", rest)
    names = [n for n in mapper.names if len(n) >= _MIN_NAME and n not in _GENERIC
             and n not in _STRUCTURAL]
    if names:
        alt = "|".join(re.escape(n) for n in names)
        found["name"] = len(set(m.group(0).lower() for m in re.finditer(
            rf"(?<![A-Za-z0-9])(?:{alt})(?![A-Za-z0-9])", rest, re.I)))
    return {k: v for k, v in found.items() if v}


def _harvest_lists(attrs: Dict[str, Any], mapper: "_Mapper") -> None:
    """Names kept inside list attributes: IAM policy and group names, the
    names of principals in other accounts, node group and Fargate profile
    names."""
    for key, prefix in (("attached_policies", "policy"), ("inline_policies", "policy"),
                        ("groups", "group")):
        for name in attrs.get(key) or []:
            mapper.want(name, prefix)
    mapper.want(attrs.get("permissions_boundary"), "policy")
    for key in ("granted_to", "granted_all_to"):
        for name in attrs.get(key) or []:
            mapper.want(name, "principal")
    for principal in attrs.get("trusted_principals") or []:
        if isinstance(principal, dict):
            mapper.want(principal.get("name"), principal.get("type") or "principal")
    for key, prefix in (("nodegroups", "nodegroup"), ("fargate_profiles", "profile")):
        for item in attrs.get(key) or []:
            if isinstance(item, dict):
                mapper.want(item.get("name"), prefix)
                for namespace in item.get("namespaces") or []:
                    mapper.want(namespace, "namespace")


def anonymize(topo: Topology, ips: str = "public", keep_names: bool = False,
              ip_key_file: Optional[str] = None, strict: bool = False) -> Topology:
    """An anonymized copy of `topo`; the input is not changed."""
    if ips not in IPS_MODES:
        raise ValueError(f"--ips must be one of {', '.join(IPS_MODES)}, not {ips!r}")
    mapper = _Mapper(ips)
    mapper.scan(topo.dumps())
    _harvest(topo, mapper, keep_names)
    data = mapper.walk(json.loads(topo.dumps()))
    if strict:
        for item in data.get("nodes", []) + data.get("links", []):
            if "attrs" in item:
                item["attrs"] = _strict(item["attrs"])
                if not item["attrs"]:
                    del item["attrs"]
    data.setdefault("meta", {})["anonymized"] = True
    if strict:
        data["meta"]["strict"] = True
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    if ips == "ipredact":
        text = _ipredact(text, ip_key_file)
    out = Topology.loads(text)
    _canonical_links(out)
    for what, count in _survivors(mapper, out.dumps()).items():
        out.warn(f"anonymize: {count} original {what}(s) are still present in the output")
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
    try:
        done = subprocess.run(command, input=text, capture_output=True, text=True,
                              timeout=IPREDACT_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ipredact took more than {IPREDACT_TIMEOUT} seconds; giving up") from None
    if done.returncode != 0:
        raise RuntimeError(f"ipredact failed: {done.stderr.strip() or done.returncode}")
    return done.stdout


def add_arguments(parser) -> None:
    parser.add_argument("input", metavar="TOPOLOGY",
                        help="topology JSON to anonymize ('-' for stdin)")
    parser.add_argument("output", nargs="*", metavar="FILE", help=OUTPUT_HELP)
    parser.add_argument("--ips", choices=IPS_MODES, default="ipredact",
                        help="addresses: ipredact (structure-preserving, needs the ipredact tool), "
                             "public (only public addresses, into documentation ranges) or keep "
                             "(default: ipredact)")
    parser.add_argument("--ip-key-file", metavar="FILE",
                        help="ipredact key file, to get the same addresses on every run")
    parser.add_argument("--strict", action="store_true",
                        help="also drop every attribute that isn't on the known, non-free-text list "
                             "(tags, descriptions, serials, anything unfamiliar)")
    parser.add_argument("--keep-names", action="store_true",
                        help="leave names and tag values alone; IDs and addresses are still replaced")


def run(args) -> Topology:
    if args.ip_key_file and args.ips != "ipredact":
        raise ValueError("--ip-key-file only applies to --ips ipredact")
    return anonymize(read_topology(args.input), ips=args.ips, keep_names=args.keep_names,
                     ip_key_file=args.ip_key_file, strict=args.strict)
