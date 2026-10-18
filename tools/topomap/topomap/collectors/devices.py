"""Crawl network devices over SSH, following CDP/LLDP neighbours.

From the seeds (`--seed`, `--inventory`) the crawler logs in to each device,
runs the read-only commands `parsers.COMMANDS` lists for its platform, and
hands the output to `parsers.parse_outputs`: the same parsers the files
collector uses, so a live crawl and a folder of saved captures draw the same
picture. Neighbours' management addresses become the next wave, but only
inside `--allow` networks and within `--max-depth` hops. Without `--allow`
nothing beyond the seeds is contacted: discovering a network must not mean
logging in to all of it unasked.

What reaches a device:

* our commands, each checked by `ensure_read_only` before anything is sent;
* netmiko's session setup (`terminal length 0` and the like);
* when no platform is given (`--platform` or the inventory), the probes of
  netmiko's `SSHDetect` (`show version`, `display version`, ...).

Passwords come only from `TOPOMAP_PASSWORD` or an `--ask-pass` prompt, never
from the command line (where shell history and `ps` would see them) or the
inventory, and are scrubbed from every warning.
"""

from __future__ import annotations

import argparse
import csv
import getpass
import io
import ipaddress
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Union

from .. import parsers
from ..facts import ROLES, DeviceFacts, facts_to_topology, guess_role, normalize_hostname
from ..model import Topology

HELP = "Crawl network devices over SSH using CDP/LLDP, running read-only show commands."
PRODUCES_TOPOLOGY = True

DEFAULT_MAX_DEPTH = 3
DEFAULT_WORKERS = 8
DEFAULT_TIMEOUT = 30.0
DEFAULT_SKIP_ROLES = ("phone", "wireless", "server")
USERNAME_ENV = "TOPOMAP_USERNAME"
PASSWORD_ENV = "TOPOMAP_PASSWORD"

# netmiko device types that use the same driver, and so the same commands, as one of ours.
_PLATFORM_ALIASES = {"cisco_xe": "cisco_ios", "juniper": "juniper_junos"}
_UNSAFE_CHARS = re.compile(r"[|;<>\r\n]")
_SECRET_KEY = re.compile(r"passw|secret|^pass$", re.IGNORECASE)


@dataclass
class Target:
    """An address to log in to, with whatever the inventory says about it."""
    host: str
    platform: str = ""        # ours or a netmiko device type; "" to autodetect
    site: str = ""
    role: str = ""
    port: int = 22
    depth: int = 0            # hops from the seeds


@dataclass
class Login:
    username: str
    password: Optional[str] = field(default=None, repr=False)
    key_file: Optional[str] = None
    timeout: float = DEFAULT_TIMEOUT
    strict_host_keys: bool = False


class UnsafeCommandError(ValueError):
    """A command that is not one of the known, plain show commands."""


@dataclass
class _Result:
    target: Target
    platform: str = ""
    outputs: Dict[str, str] = field(default_factory=dict)
    facts: Optional[DeviceFacts] = None
    error: str = ""
    contacted: bool = False


# -- platforms and the read-only guard -------------------------------------------

def map_platform(name: Optional[str]) -> Optional[str]:
    """Our platform for a netmiko device type (or our own name), or None if
    the parsers can't read that platform's output."""
    if not name:
        return None
    name = name.strip().lower()
    name = _PLATFORM_ALIASES.get(name, name)
    return name if name in parsers.COMMANDS else None


def ensure_read_only(platform: str, commands: Sequence[str]) -> None:
    """Refuse anything but the known, plain show commands for `platform`.

    `parsers.COMMANDS` is the only source of commands, so this is defence in
    depth: a bad edit there must not turn a crawl into a change made on
    every device it reaches. Pipes, separators and redirection are refused
    even in a listed command, since they are how a "show" grows side effects.
    """
    allowed = parsers.COMMANDS.get(platform, ())
    for command in commands:
        if (not isinstance(command, str) or command not in allowed
                or not command.startswith("show ") or _UNSAFE_CHARS.search(command)):
            raise UnsafeCommandError(
                f"refusing to run {command!r}: not a read-only show command for {platform}")


# -- one device --------------------------------------------------------------------

def _collect(target: Target, default_platform: str, login: Login, connect: Callable,
             detect: Callable, parse: Callable) -> _Result:
    """Log in to one target and parse what it says. Runs in a worker thread,
    so it touches no shared state and reports failure in its result."""
    result = _Result(target)
    try:
        requested = target.platform or default_platform
        if requested:
            result.platform = map_platform(requested) or ""
            if not result.platform:
                result.error = f"unsupported platform {requested!r}"
                return result
        else:
            result.contacted = True
            detected = detect(target, login)
            result.platform = map_platform(detected) or ""
            if not result.platform:
                result.error = (f"unsupported platform {detected!r}" if detected
                                else "could not detect the platform")
                return result
        commands = list(parsers.COMMANDS[result.platform])
        ensure_read_only(result.platform, commands)
        result.contacted = True
        session = connect(target, result.platform, login)
        try:
            outputs = {command: session.send_command(command) for command in commands}
        finally:
            _close(session)
        result.outputs = outputs
    except Exception as e:
        result.error = _describe(e)
        return result
    try:
        result.facts = parse(result.outputs, result.platform, source=f"ssh://{target.host}")
    except Exception as e:
        result.error = f"parse error: {_describe(e)}"
    return result


def _describe(e: BaseException) -> str:
    """One line of reason. netmiko's messages run to a page of advice, and
    callers match on these short forms."""
    if isinstance(e, UnsafeCommandError):
        return str(e)
    name = type(e).__name__
    if "authentication" in name.lower():
        return "authentication failed"
    if "timeout" in name.lower() or isinstance(e, TimeoutError):
        return "timed out"
    lines = str(e).strip().splitlines()
    return f"{name}: {lines[0]}" if lines else name


def _close(session: Any) -> None:
    if session is None:
        return
    try:
        session.disconnect()
    except Exception:
        pass  # the outputs are already in hand; a messy logout changes nothing


# -- the crawl ---------------------------------------------------------------------

class _CrawlState:
    """Bookkeeping for one crawl. Only the main thread touches it."""

    def __init__(self, login: Login, allow: Sequence[str], max_depth: int, site: str,
                 skip_roles: Sequence[str], save_raw: Optional[str]):
        self.login = login
        self.allow = [ipaddress.ip_network(str(a), strict=False) for a in allow]
        self.max_depth = max_depth
        self.site = site
        self.skip_roles = set(skip_roles)
        self.save_raw = save_raw
        self.addresses: Set[str] = set()   # every address queued so far
        self.collected: Set[str] = set()   # hostnames collected
        self.expected: Set[str] = set()    # hostnames queued from a neighbour report
        self.facts: List[DeviceFacts] = []
        self.warnings: List[str] = []
        self.contacted = 0
        self.failed = 0

    def warn(self, message: str) -> None:
        if self.login.password:
            message = message.replace(self.login.password, "***")
        self.warnings.append(message)

    def admit_seeds(self, seeds: Iterable[Union[Target, str]]) -> List[Target]:
        wave = []
        for seed in seeds:
            target = Target(seed) if isinstance(seed, str) else seed
            key = _address_key(target.host)
            if key not in self.addresses:
                self.addresses.add(key)
                wave.append(target)
        return wave

    def accept(self, r: _Result) -> bool:
        """Record one result; True if it is a device not collected before."""
        host = r.target.host
        self.contacted += r.contacted
        if r.error:
            self.failed += 1
            self.warn(f"{host}: {r.error}")
            self._save(host, host, r.outputs)
            return False
        facts = r.facts
        if not facts.hostname:
            facts.hostname = host
            self.warn(f"{host}: no hostname in the output; using the address")
        name = normalize_hostname(facts.hostname)
        if name in self.collected:
            return False  # the same device, reached at another of its addresses
        self.collected.add(name)
        facts.mgmt_ip = host
        facts.site = r.target.site or self.site or facts.site
        facts.role = r.target.role or facts.role
        facts.platform = facts.platform or r.platform
        facts.source = facts.source or f"ssh://{host}"
        self.facts.append(facts)
        self._save(host, facts.hostname, r.outputs)
        return True

    def next_targets(self, facts: DeviceFacts, depth: int) -> List[Target]:
        """Neighbours to visit next: inside --allow, within --max-depth, not a
        skipped role, and not already seen by address or by hostname."""
        if not self.allow or depth > self.max_depth:
            return []
        wave = []
        for n in facts.neighbors:
            ip = _loginable(n.remote_mgmt_ip)
            if ip is None or not any(ip in net for net in self.allow):
                continue
            name = normalize_hostname(n.remote_name) if n.remote_name else ""
            if str(ip) in self.addresses or (name and (name in self.collected or name in self.expected)):
                continue
            if guess_role(n.remote_platform, n.remote_capabilities, n.remote_name) in self.skip_roles:
                continue
            self.addresses.add(str(ip))
            if name:
                self.expected.add(name)
            wave.append(Target(str(ip), depth=depth))
        return wave

    def _save(self, host: str, hostname: str, outputs: Dict[str, str]) -> None:
        if not self.save_raw or not outputs:
            return
        try:
            write_capture(self.save_raw, hostname, outputs)
        except OSError as e:
            self.warn(f"{host}: could not save raw output: {e}")


def crawl(seeds: Iterable[Union[Target, str]], login: Login, *, allow: Sequence[str] = (),
          max_depth: int = DEFAULT_MAX_DEPTH, workers: int = DEFAULT_WORKERS, platform: str = "",
          site: str = "", skip_roles: Sequence[str] = DEFAULT_SKIP_ROLES,
          save_raw: Optional[str] = None, connect: Optional[Callable] = None,
          detect: Optional[Callable] = None, parse: Optional[Callable] = None) -> Topology:
    """Breadth-first crawl from `seeds`; the topology of everything collected.

    Each wave runs in a thread pool but is processed in order once it is
    complete, so which address wins a duplicate, and so the output, doesn't
    depend on which device answered first.

    The three hooks default to netmiko and the real parsers; tests pass fakes.
    `connect(target, platform, login)` returns a session with
    `send_command(command) -> str` and `disconnect()`; `detect(target, login)`
    returns a netmiko device type or None; `parse` is `parsers.parse_outputs`.
    """
    if connect is None or detect is None:
        _netmiko()  # fail once, before the first login, rather than once per device
    connect = connect or netmiko_connect
    detect = detect or netmiko_detect
    parse = parse or parsers.parse_outputs
    if save_raw:
        os.makedirs(save_raw, mode=0o700, exist_ok=True)

    state = _CrawlState(login, allow, max_depth, site, skip_roles, save_raw)
    wave = state.admit_seeds(seeds)
    seed_hosts = [t.host for t in wave]

    def collect(target: Target) -> _Result:
        return _collect(target, platform, login, connect, detect, parse)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        while wave:
            results = list(pool.map(collect, wave))
            accepted = [r for r in results if state.accept(r)]
            wave = [t for r in accepted for t in state.next_targets(r.facts, r.target.depth + 1)]

    topo = facts_to_topology(state.facts)
    topo.meta = {"collector": "devices", "seeds": seed_hosts, "contacted": state.contacted,
                 "failed": state.failed,
                 "collected_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    for warning in state.warnings:
        topo.warn(warning)
    return topo


def _address_key(host: str) -> str:
    try:
        return str(ipaddress.ip_address(host.strip()))
    except ValueError:
        return host.strip().lower().rstrip(".")


def _loginable(value: Optional[str]) -> Optional[Union[ipaddress.IPv4Address, ipaddress.IPv6Address]]:
    """The address if it is one we could log in to. LLDP sometimes advertises
    0.0.0.0, a loopback or a link-local address, none of which lead anywhere."""
    try:
        ip = ipaddress.ip_address(str(value or "").strip())
    except ValueError:
        return None
    if ip.is_unspecified or ip.is_loopback or ip.is_multicast or ip.is_link_local:
        return None
    return ip


# -- raw captures --------------------------------------------------------------------

def write_capture(directory: str, hostname: str, outputs: Mapping[str, str]) -> str:
    """Save outputs as a capture the files collector reads back: each output
    after a "hostname#command" prompt line. Mode 0600, because show output
    maps every address and serial number on the network."""
    name = _safe_name(hostname)
    path = os.path.join(directory, f"{name}.txt")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        os.chmod(path, 0o600)  # os.open's mode only applies to a file it creates
        for command, output in outputs.items():
            f.write(f"{name}#{command}\n{output.rstrip()}\n")
    return path


def _safe_name(hostname: str) -> str:
    """A file name and prompt from a name the device chose: no path
    separators or other surprises."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", normalize_hostname(hostname)).strip("._") or "device"


# -- netmiko -------------------------------------------------------------------------

def _netmiko():
    try:
        import netmiko
    except ImportError:
        raise RuntimeError("collecting from devices needs netmiko: "
                           "pip install 'topomap[devices]'") from None
    return netmiko


def _netmiko_params(target: Target, login: Login) -> Dict[str, Any]:
    """Connection settings shared by detection and collection. Keys and the
    SSH agent are offered only without a password, or with --key-file: a long
    agent keyring can otherwise use up a device's MaxAuthTries before the
    password is ever tried."""
    params: Dict[str, Any] = {
        "host": target.host, "port": target.port, "username": login.username,
        "conn_timeout": login.timeout, "auth_timeout": login.timeout,
        "banner_timeout": login.timeout,
        "ssh_strict": login.strict_host_keys, "system_host_keys": login.strict_host_keys,
    }
    if login.password is not None:
        params["password"] = login.password
    if login.key_file or login.password is None:
        params.update(use_keys=True, allow_agent=True)
    if login.key_file:
        params["key_file"] = login.key_file
    return params


class _NetmikoSession:
    def __init__(self, connection: Any, timeout: float):
        self._connection = connection
        self._timeout = timeout

    def send_command(self, command: str) -> str:
        return self._connection.send_command(command, read_timeout=self._timeout)

    def disconnect(self) -> None:
        self._connection.disconnect()


def netmiko_connect(target: Target, platform: str, login: Login) -> _NetmikoSession:
    connection = _netmiko().ConnectHandler(device_type=platform, **_netmiko_params(target, login))
    return _NetmikoSession(connection, login.timeout)


def netmiko_detect(target: Target, login: Login) -> Optional[str]:
    guesser = _netmiko().SSHDetect(device_type="autodetect", **_netmiko_params(target, login))
    try:
        return guesser.autodetect()
    finally:
        _close(getattr(guesser, "connection", None))


# -- inventory and login ---------------------------------------------------------------

def load_inventory(path: str) -> List[Target]:
    """Targets from a CSV file with a header row, or a JSON list of objects
    (or bare host strings). Keys: host, and optionally platform, site, role,
    port; others are ignored.

    A filled-in password column is refused rather than ignored: credentials
    in a file that gets copied around and committed are exactly the leak
    this tool must not encourage."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        text = f.read()
    if path.lower().endswith(".json") or text.lstrip().startswith(("[", "{")):
        try:
            rows = json.loads(text)
        except ValueError as e:
            raise ValueError(f"{path}: not valid JSON: {e}") from None
        if not isinstance(rows, list):
            raise ValueError(f"{path}: expected a JSON list of devices")
    else:
        reader = csv.DictReader(io.StringIO(text))
        if "host" not in [(k or "").strip().lower() for k in reader.fieldnames or []]:
            raise ValueError(f"{path}: needs a header row with a 'host' column")
        rows = list(reader)
    targets = []
    for i, row in enumerate(rows, 1):
        target = _inventory_target(row, f"{path}: entry {i}")
        if target is not None:
            targets.append(target)
    return targets


def _inventory_target(row: Any, where: str) -> Optional[Target]:
    if isinstance(row, str):
        row = {"host": row}
    if not isinstance(row, dict):
        raise ValueError(f"{where}: expected an object with a 'host'")
    row = {str(k).strip().lower(): "" if v is None else str(v).strip()
           for k, v in row.items() if k is not None}
    secrets = [k for k, v in row.items() if v and _SECRET_KEY.search(k)]
    if secrets:
        raise ValueError(f"{where}: inventories must not hold passwords ({secrets[0]!r}); "
                         f"use {PASSWORD_ENV} or --ask-pass")
    host = row.get("host", "")
    if not host or host.startswith("#"):
        return None
    role = row.get("role", "").lower()
    if role and role not in ROLES:
        raise ValueError(f"{where}: unknown role {role!r} (choose from {', '.join(ROLES)})")
    platform = row.get("platform", "").lower()
    if platform in ("auto", "autodetect"):
        platform = ""
    return Target(host, platform=platform, site=row.get("site", ""), role=role,
                  port=_port(row.get("port", ""), where))


def _port(value: str, where: str) -> int:
    if not value:
        return 22
    try:
        port = int(value)
    except ValueError:
        port = 0
    if not 0 < port < 65536:
        raise ValueError(f"{where}: bad port {value!r}")
    return port


def login_from_args(args: argparse.Namespace, environ: Optional[Mapping[str, str]] = None) -> Login:
    """Credentials from the environment, a prompt and a key file; never from
    argv, which ends up in shell history and in every `ps` listing."""
    env = os.environ if environ is None else environ
    username = args.username or env.get(USERNAME_ENV) or _os_user()
    if args.ask_pass:
        password = getpass.getpass(f"SSH password for {username}: ") or None
    else:
        password = env.get(PASSWORD_ENV) or None
    key_file = os.path.expanduser(args.key_file) if args.key_file else None
    if key_file and not os.path.isfile(key_file):
        raise ValueError(f"--key-file {args.key_file}: no such file")
    return Login(username, password, key_file, args.timeout, args.strict_host_keys)


def _os_user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        raise ValueError(f"no username: give --username or set {USERNAME_ENV}") from None


# -- command line ----------------------------------------------------------------------

def add_arguments(parser: argparse.ArgumentParser) -> None:
    what = parser.add_argument_group("what to crawl")
    what.add_argument("--seed", action="append", default=[], metavar="HOST",
                      help="a device to start from (repeatable)")
    what.add_argument("--inventory", metavar="FILE",
                      help="CSV or JSON list of devices: host, and optionally platform, site, "
                           "role, port (never passwords)")
    what.add_argument("--allow", action="append", default=[], type=_cidr, metavar="CIDR",
                      help="also crawl discovered neighbours whose management address is in "
                           "CIDR (repeatable); without --allow only the seeds and inventory "
                           "hosts are contacted")
    what.add_argument("--max-depth", type=_count(0), default=DEFAULT_MAX_DEPTH, metavar="N",
                      help=f"hops beyond the seeds to crawl (default: {DEFAULT_MAX_DEPTH})")
    what.add_argument("--skip-role", action="append", choices=ROLES + ("none",), metavar="ROLE",
                      help="don't crawl neighbours that look like ROLE (repeatable; default: "
                           f"{', '.join(DEFAULT_SKIP_ROLES)}; 'none' crawls every role)")
    what.add_argument("--platform", type=_platform_arg,
                      help="platform of every device the inventory doesn't give one for: "
                           f"{', '.join(parsers.PLATFORMS)} (default: autodetect)")
    what.add_argument("--site", metavar="NAME",
                      help="put every device in this site, unless the inventory says otherwise")

    login = parser.add_argument_group(
        "login", f"The password comes from ${PASSWORD_ENV} or --ask-pass, never from the "
                 "command line. With neither, SSH keys and the agent are used.")
    login.add_argument("--username",
                       help=f"SSH username (default: ${USERNAME_ENV}, else the current user)")
    login.add_argument("--ask-pass", action="store_true", help="prompt for the SSH password")
    login.add_argument("--key-file", metavar="FILE",
                       help="SSH private key (keys in the SSH agent are tried too)")
    login.add_argument("--strict-host-keys", action="store_true",
                       help="refuse devices whose host key isn't in ~/.ssh/known_hosts")

    running = parser.add_argument_group("running")
    running.add_argument("--workers", type=_count(1), default=DEFAULT_WORKERS, metavar="N",
                         help=f"devices to talk to at once (default: {DEFAULT_WORKERS})")
    running.add_argument("--timeout", type=_seconds, default=DEFAULT_TIMEOUT, metavar="SECONDS",
                         help=f"login and per-command timeout (default: {DEFAULT_TIMEOUT:g})")
    running.add_argument("--save-raw", metavar="DIR",
                         help="also save each device's output to DIR/<hostname>.txt (mode 0600), "
                              "for 'topomap collect files'")


def run(args: argparse.Namespace) -> Topology:
    targets = load_inventory(args.inventory) if args.inventory else []
    targets += [Target(h.strip()) for h in args.seed if h.strip()]
    if not targets:
        raise ValueError("nothing to crawl: give --seed HOST or --inventory FILE")
    login = login_from_args(args)
    skip = DEFAULT_SKIP_ROLES if args.skip_role is None else args.skip_role
    return crawl(targets, login, allow=args.allow, max_depth=args.max_depth,
                 workers=args.workers, platform=args.platform or "", site=args.site or "",
                 skip_roles=[r for r in skip if r != "none"], save_raw=args.save_raw)


def _cidr(value: str) -> str:
    try:
        return str(ipaddress.ip_network(value, strict=False))
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a network: {value!r}") from None


def _count(minimum: int) -> Callable[[str], int]:
    def parse(value: str) -> int:
        try:
            n = int(value)
        except ValueError:
            n = minimum - 1
        if n < minimum:
            raise argparse.ArgumentTypeError(f"must be a whole number >= {minimum}: {value!r}")
        return n
    return parse


def _seconds(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        seconds = 0.0
    if not seconds > 0:
        raise argparse.ArgumentTypeError(f"must be a positive number of seconds: {value!r}")
    return seconds


def _platform_arg(value: str) -> str:
    platform = map_platform(value)
    if platform is None:
        raise argparse.ArgumentTypeError(
            f"unsupported platform {value!r} (choose from {', '.join(parsers.PLATFORMS)})")
    return platform
