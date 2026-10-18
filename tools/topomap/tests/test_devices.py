"""The live SSH crawler, against a fake network: no device, no netmiko and no
finished parsers needed. One integration test runs the real parsers on canned
output in fixtures/crawl/ and skips while they are still stubs."""

import argparse
import contextlib
import io
import json
import os
import re
import stat
import sys
import tempfile
import threading
import types
import unittest
from copy import deepcopy
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from topomap import cli, parsers  # noqa: E402
from topomap.collectors import devices  # noqa: E402
from topomap.collectors.devices import (Login, Target, UnsafeCommandError, crawl,  # noqa: E402
                                        ensure_read_only, load_inventory, login_from_args,
                                        map_platform)
from topomap.facts import DeviceFacts, Neighbor  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "crawl")
PASSWORD = "s3cret-Pa55word"
LOGIN = Login("netops", PASSWORD)


# netmiko's exception names, which the crawler turns into short reasons.
class NetmikoAuthenticationException(Exception):
    pass


class NetmikoTimeoutException(Exception):
    pass


def nbr(name, ip=None, platform="", caps=()):
    return Neighbor("Gi0/0", name, "Gi0/1", ip, platform, list(caps), "cdp")


class FakeNetwork:
    """Devices by address. A session answers every command with
    "<hostname>|<command>", so `parse` can tell which device it came from.
    Every login, detection and command is recorded."""

    def __init__(self, devices_by_address, errors=None, detected=None):
        self.devices = devices_by_address
        self.by_name = {f.hostname: f for f in devices_by_address.values()}
        self.errors = errors or {}
        self.detected = detected or {}
        self.logins, self.detections, self.sent = [], [], []
        self.lock = threading.Lock()

    def connect(self, target, platform, login):
        with self.lock:
            self.logins.append(target.host)
        if target.host in self.errors:
            raise self.errors[target.host]
        if target.host not in self.devices:
            raise NetmikoTimeoutException("TCP connection to device failed.\n\nCommon causes...")
        return FakeSession(self, target.host)

    def detect(self, target, login):
        with self.lock:
            self.detections.append(target.host)
        return self.detected.get(target.host, "cisco_ios")

    def parse(self, outputs, platform, source=""):
        facts = deepcopy(self.by_name[outputs["show version"].split("|")[0]])
        facts.source = source
        return facts


class FakeSession:
    def __init__(self, network, host):
        self.network, self.host = network, host

    def send_command(self, command):
        with self.network.lock:
            self.network.sent.append((self.host, command))
        return f"{self.network.devices[self.host].hostname}|{command}"

    def disconnect(self):
        pass


def fake_netmiko(network):
    """A stand-in netmiko module backed by `network`; `calls` records the
    keyword arguments ConnectHandler and SSHDetect were given."""
    module = types.ModuleType("netmiko")
    module.calls = []

    class ConnectHandler:
        def __init__(self, **kwargs):
            module.calls.append(("connect", kwargs))
            self.session = network.connect(Target(kwargs["host"], port=kwargs["port"]),
                                           kwargs["device_type"], None)

        def send_command(self, command, read_timeout=None):
            return self.session.send_command(command)

        def disconnect(self):
            pass

    class SSHDetect:
        def __init__(self, **kwargs):
            module.calls.append(("detect", kwargs))
            self.host = kwargs["host"]
            self.connection = mock.Mock()

        def autodetect(self):
            return network.detect(Target(self.host), None)

    module.ConnectHandler = ConnectHandler
    module.SSHDetect = SSHDetect
    return module


def chain_network(**kw):
    """a - b - c - d - e in a line, plus things a sees but shouldn't crawl:
    a router outside the allow-list and an IP phone."""
    return FakeNetwork({
        "10.0.0.1": DeviceFacts("a", neighbors=[
            nbr("b.example.net", "10.0.0.2"), nbr("x.example.net", "192.0.2.9"),
            nbr("SEP00005E0053AA", "10.0.0.50", "Cisco IP Phone 8865", ["Host", "Phone"])]),
        "10.0.0.2": DeviceFacts("b", neighbors=[nbr("a", "10.0.0.1"), nbr("c", "10.0.0.3")]),
        "10.0.0.3": DeviceFacts("c", neighbors=[nbr("b", "10.0.0.2"), nbr("d", "10.0.0.4")]),
        "10.0.0.4": DeviceFacts("d", neighbors=[nbr("c", "10.0.0.3"), nbr("e", "10.0.0.5")]),
        "10.0.0.5": DeviceFacts("e", neighbors=[nbr("d", "10.0.0.4")]),
    }, **kw)


def run_crawl(network, seeds, **kw):
    kw.setdefault("platform", "cisco_ios")
    return crawl(seeds, LOGIN, connect=network.connect, detect=network.detect,
                 parse=network.parse, **kw)


def collected(topo):
    return sorted(n.label for n in topo.by_kind("device") if "discovered" not in n.attrs)


PROMPT = re.compile(r"^[\w.-]+#(show .*)$")


def read_capture(path):
    """Split a capture back into {command: output}."""
    outputs, command = {}, None
    with open(path, encoding="utf-8") as f:
        for line in f.read().splitlines():
            m = PROMPT.match(line)
            if m:
                command = m.group(1)
                outputs[command] = []
            elif command is not None:
                outputs[command].append(line)
    return {c: "\n".join(lines) for c, lines in outputs.items()}


class ScopeTest(unittest.TestCase):
    def test_without_allow_only_seeds_are_contacted(self):
        network = chain_network()
        topo = run_crawl(network, ["10.0.0.1"], max_depth=10)
        self.assertEqual(network.logins, ["10.0.0.1"])
        self.assertEqual(collected(topo), ["a"])
        self.assertEqual(topo.nodes["net:device:b"].attrs["discovered"], "neighbor")
        self.assertEqual(topo.validate(), [])
        meta = topo.meta
        self.assertEqual((meta["collector"], meta["seeds"], meta["contacted"], meta["failed"]),
                         ("devices", ["10.0.0.1"], 1, 0))
        self.assertRegex(meta["collected_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d")

    def test_allow_list_and_depth(self):
        network = chain_network()
        topo = run_crawl(network, ["10.0.0.1"], allow=["10.0.0.0/16"], max_depth=2)
        self.assertEqual(sorted(network.logins), ["10.0.0.1", "10.0.0.2", "10.0.0.3"])
        self.assertNotIn("192.0.2.9", network.logins)  # outside --allow
        self.assertEqual(collected(topo), ["a", "b", "c"])
        self.assertEqual(topo.nodes["net:device:d"].attrs["discovered"], "neighbor")
        self.assertEqual(topo.nodes["net:device:c"].attrs["mgmt_ip"], "10.0.0.3")

    def test_depth_zero_and_narrow_allow(self):
        network = chain_network()
        run_crawl(network, ["10.0.0.1"], allow=["10.0.0.0/8"], max_depth=0)
        self.assertEqual(network.logins, ["10.0.0.1"])
        network = chain_network()
        run_crawl(network, ["10.0.0.1"], allow=["10.0.0.2/32", "2001:db8::/32"])
        self.assertEqual(network.logins, ["10.0.0.1", "10.0.0.2"])

    def test_skip_roles(self):
        network = chain_network()
        run_crawl(network, ["10.0.0.1"], allow=["10.0.0.0/24"], max_depth=1)
        self.assertNotIn("10.0.0.50", network.logins)
        network = chain_network()
        topo = run_crawl(network, ["10.0.0.1"], allow=["10.0.0.0/24"], max_depth=1, skip_roles=())
        self.assertIn("10.0.0.50", network.logins)
        self.assertIn("10.0.0.50: timed out", topo.meta["warnings"])


class DedupeTest(unittest.TestCase):
    def test_by_address(self):
        network = chain_network()
        run_crawl(network, ["10.0.0.1", "10.0.0.1", "10.0.0.2"], allow=["10.0.0.0/24"])
        self.assertEqual(len(network.logins), len(set(network.logins)))
        self.assertEqual(sorted(network.logins), [f"10.0.0.{i}" for i in range(1, 6)])

    def test_same_device_at_two_seed_addresses(self):
        network = chain_network()
        network.devices["10.0.1.1"] = network.devices["10.0.0.1"]
        topo = run_crawl(network, ["10.0.0.1", "10.0.1.1"])
        self.assertEqual(collected(topo), ["a"])
        self.assertEqual(topo.nodes["net:device:a"].attrs["mgmt_ip"], "10.0.0.1")
        self.assertEqual(topo.meta["contacted"], 2)

    def test_neighbour_reported_at_two_addresses(self):
        p, q = DeviceFacts("p", neighbors=[nbr("q.example.net", "10.0.2.2")]), DeviceFacts("q")
        network = FakeNetwork({
            "10.0.2.1": p,
            "10.0.2.3": DeviceFacts("r", neighbors=[nbr("Q", "10.0.2.22"),
                                                    nbr("p", "10.0.2.99")]),
            "10.0.2.2": q, "10.0.2.22": q, "10.0.2.99": p,
        })
        topo = run_crawl(network, ["10.0.2.1", "10.0.2.3"], allow=["10.0.2.0/24"])
        self.assertEqual(sorted(network.logins), ["10.0.2.1", "10.0.2.2", "10.0.2.3"])
        self.assertEqual(collected(topo), ["p", "q", "r"])


class FailureTest(unittest.TestCase):
    def test_failures_warn_and_others_continue(self):
        network = chain_network(errors={
            "10.0.0.1": NetmikoAuthenticationException("Authentication to device failed."),
            "10.0.0.4": RuntimeError(f"a library that echoed {PASSWORD}\nand more"),
        })
        topo = run_crawl(network, ["10.0.0.1", "10.0.0.2", "10.0.0.4", "10.0.0.9"])
        self.assertEqual(collected(topo), ["b"])
        self.assertEqual(topo.meta["warnings"], [
            "10.0.0.1: authentication failed",
            "10.0.0.4: RuntimeError: a library that echoed ***",
            "10.0.0.9: timed out",
        ])
        self.assertEqual((topo.meta["contacted"], topo.meta["failed"]), (4, 3))
        self.assertNotIn(PASSWORD, topo.dumps())

    def test_parse_error(self):
        network = chain_network()

        def parse(outputs, platform, source=""):
            if outputs["show version"].startswith("b|"):
                raise ValueError("unexpected banner")
            return network.parse(outputs, platform, source)

        topo = crawl(["10.0.0.1", "10.0.0.2"], LOGIN, platform="cisco_ios",
                     connect=network.connect, detect=network.detect, parse=parse)
        self.assertEqual(collected(topo), ["a"])
        self.assertEqual(topo.meta["warnings"],
                         ["10.0.0.2: parse error: ValueError: unexpected banner"])

    def test_missing_hostname_uses_address(self):
        network = FakeNetwork({"10.0.3.1": DeviceFacts("h")})
        topo = crawl(["10.0.3.1"], LOGIN, platform="cisco_ios", connect=network.connect,
                     detect=network.detect, parse=lambda o, p, source="": DeviceFacts(""))
        self.assertIn("net:device:10.0.3.1", topo.nodes)
        self.assertEqual(len(topo.meta["warnings"]), 1)


class GuardTest(unittest.TestCase):
    def test_known_commands_pass(self):
        for platform, commands in parsers.COMMANDS.items():
            ensure_read_only(platform, commands)

    def test_refuses_anything_else(self):
        bad = ["configure terminal", "show running-config | include username",
               "show version; reload", "show version\nreload", "show tech > flash:x",
               "show tech >> flash:x", "show version < x", " show version", "reload"]
        with mock.patch.dict(parsers.COMMANDS, {"cisco_ios": ["show version"] + bad}):
            for command in bad:
                with self.assertRaises(UnsafeCommandError, msg=command):
                    ensure_read_only("cisco_ios", [command])
        with self.assertRaises(UnsafeCommandError):
            ensure_read_only("cisco_ios", ["show clock"])  # a show, but not a listed one
        with self.assertRaises(UnsafeCommandError):
            ensure_read_only("linux", ["show version"])

    def test_crawler_sends_nothing_when_a_command_is_unsafe(self):
        network = chain_network()
        unsafe = {"cisco_ios": ["show version", "show running-config | include username"]}
        with mock.patch.dict(parsers.COMMANDS, unsafe):
            topo = run_crawl(network, ["10.0.0.1"])
        self.assertEqual((network.logins, network.sent), ([], []))
        self.assertEqual(topo.meta["warnings"], [
            "10.0.0.1: refusing to run 'show running-config | include username': "
            "not a read-only show command for cisco_ios"])
        self.assertEqual((topo.meta["contacted"], topo.meta["failed"]), (0, 1))

    def test_only_listed_commands_are_sent(self):
        network = chain_network()
        run_crawl(network, ["10.0.0.1"])
        self.assertEqual([c for _, c in network.sent], parsers.COMMANDS["cisco_ios"])


class InventoryTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name

    def write(self, name, text):
        path = os.path.join(self.dir, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_csv(self):
        path = self.write("inv.csv", "host,platform,site,role,port,notes\n"
                                     "10.0.0.1,cisco_ios,Branch-7,router,22,core\n"
                                     "# a comment,,,,,\n"
                                     "10.0.0.2,,,,2222,\n\n")
        targets = load_inventory(path)
        self.assertEqual(targets, [Target("10.0.0.1", "cisco_ios", "Branch-7", "router", 22),
                                   Target("10.0.0.2", port=2222)])

    def test_json(self):
        path = self.write("inv.json", json.dumps([
            {"host": "10.0.0.1", "platform": "cisco_xe", "site": "HQ", "role": "Switch"},
            "10.0.0.2", {"host": "10.0.0.3", "port": 2222, "platform": "autodetect"}]))
        self.assertEqual(load_inventory(path), [
            Target("10.0.0.1", "cisco_xe", "HQ", "switch"), Target("10.0.0.2"),
            Target("10.0.0.3", port=2222)])

    def test_refuses_passwords_and_bad_values(self):
        cases = {"inv.csv": "host,password\n10.0.0.1,hunter2-example\n",
                 "inv.json": json.dumps([{"host": "10.0.0.1", "enable_secret": "hunter2-example"}])}
        for name, text in cases.items():
            with self.assertRaises(ValueError) as e:
                load_inventory(self.write(name, text))
            self.assertIn("passwords", str(e.exception))
            self.assertNotIn("hunter2", str(e.exception))
        for text in ("host,role\n10.0.0.1,toaster\n", "host,port\n10.0.0.1,99999\n",
                     "address\n10.0.0.1\n", '{"host": "10.0.0.1"}'):
            with self.assertRaises(ValueError, msg=text):
                load_inventory(self.write("bad.txt", text))
        self.assertEqual(len(load_inventory(self.write("ok.csv", "host,password\n10.0.0.1,\n"))), 1)

    def test_inventory_overrides_and_platforms(self):
        network = chain_network(detected={"10.0.0.2": "arista_eos"})
        network.devices["10.0.0.1"].site = "parsed-site"
        network.devices["10.0.0.1"].role = "switch"
        targets = [Target("10.0.0.1", "cisco_xe", "Branch-7", "router"), Target("10.0.0.2"),
                   Target("10.0.0.3", platform="fortinet")]
        topo = crawl(targets, LOGIN, site="Lab", connect=network.connect, detect=network.detect,
                     parse=network.parse)
        self.assertEqual(network.detections, ["10.0.0.2"])  # 10.0.0.1's platform was given
        self.assertNotIn("10.0.0.3", network.logins)
        a, b = topo.nodes["net:device:a"], topo.nodes["net:device:b"]
        self.assertEqual((a.parent, a.attrs["role"], a.attrs["platform"]),
                         ("net:site:branch-7", "router", "cisco_ios"))
        self.assertEqual((b.parent, b.attrs["platform"]), ("net:site:lab", "arista_eos"))
        self.assertEqual(topo.meta["warnings"], ["10.0.0.3: unsupported platform 'fortinet'"])
        self.assertEqual((topo.meta["contacted"], topo.meta["failed"]), (2, 1))


class LoginTest(unittest.TestCase):
    def parser(self):
        parser = argparse.ArgumentParser()
        devices.add_arguments(parser)
        return parser

    def test_password_from_env(self):
        args = self.parser().parse_args(["--seed", "10.0.0.1"])
        login = login_from_args(args, {"TOPOMAP_PASSWORD": PASSWORD, "TOPOMAP_USERNAME": "alice"})
        self.assertEqual((login.username, login.password), ("alice", PASSWORD))
        self.assertNotIn(PASSWORD, repr(login))

    def test_ask_pass(self):
        args = self.parser().parse_args(["--ask-pass", "--username", "bob"])
        with mock.patch("getpass.getpass", return_value="typed-in") as prompt:
            login = login_from_args(args, {"TOPOMAP_PASSWORD": "from-env"})
        prompt.assert_called_once()
        self.assertEqual((login.username, login.password), ("bob", "typed-in"))

    def test_defaults_to_os_user_and_keys(self):
        with mock.patch("getpass.getuser", return_value="carol"):
            login = login_from_args(self.parser().parse_args([]), {})
        self.assertEqual((login.username, login.password), ("carol", None))

    def test_no_password_option(self):
        parser = self.parser()
        options = [o for a in parser._actions for o in a.option_strings]
        self.assertEqual([o for o in options if "pass" in o], ["--ask-pass"])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parser.parse_args(["--password", PASSWORD])

    def test_netmiko_params(self):
        params = devices._netmiko_params(Target("10.0.0.1", port=2222), LOGIN)
        self.assertEqual((params["password"], params["port"]), (PASSWORD, 2222))
        self.assertNotIn("allow_agent", params)  # an agent keyring could lock the account out
        params = devices._netmiko_params(Target("10.0.0.1"), Login("netops", key_file="/k"))
        self.assertNotIn("password", params)
        self.assertEqual((params["use_keys"], params["allow_agent"], params["key_file"]),
                         (True, True, "/k"))


class PlatformTest(unittest.TestCase):
    def test_mapping(self):
        cases = {"cisco_ios": "cisco_ios", "cisco_xe": "cisco_ios", "cisco_nxos": "cisco_nxos",
                 "arista_eos": "arista_eos", "juniper_junos": "juniper_junos",
                 "juniper": "juniper_junos", "linux": None, "cisco_xr": None, "": None, None: None}
        for netmiko_type, ours in cases.items():
            self.assertEqual(map_platform(netmiko_type), ours, netmiko_type)

    def test_autodetect_with_sshdetect(self):
        facts = {f"10.0.0.{i}": DeviceFacts(f"h{i}") for i in range(1, 5)}
        network = FakeNetwork(facts, detected={"10.0.0.1": "cisco_xe", "10.0.0.2": "juniper",
                                               "10.0.0.3": "linux", "10.0.0.4": None})
        module = fake_netmiko(network)
        with mock.patch.dict(sys.modules, {"netmiko": module}):
            topo = crawl(sorted(facts), LOGIN, parse=network.parse)
        connects = {kw["host"]: kw["device_type"] for kind, kw in module.calls if kind == "connect"}
        self.assertEqual(connects, {"10.0.0.1": "cisco_ios", "10.0.0.2": "juniper_junos"})
        detects = [kw for kind, kw in module.calls if kind == "detect"]
        self.assertEqual({kw["device_type"] for kw in detects}, {"autodetect"})
        self.assertEqual(topo.meta["warnings"], ["10.0.0.3: unsupported platform 'linux'",
                                                 "10.0.0.4: could not detect the platform"])
        self.assertEqual(network.sent.count(("10.0.0.2", "show interfaces terse")), 1)


class SaveRawTest(unittest.TestCase):
    def test_capture_contents_and_mode(self):
        network = chain_network()
        with tempfile.TemporaryDirectory() as d:
            raw = os.path.join(d, "raw")
            os.makedirs(raw)
            stale = os.path.join(raw, "a.txt")
            with open(stale, "w") as f:
                f.write("stale\n")
            os.chmod(stale, 0o644)
            run_crawl(network, ["10.0.0.1", "10.0.0.2"], save_raw=raw)
            self.assertEqual(sorted(os.listdir(raw)), ["a.txt", "b.txt"])
            with open(stale, encoding="utf-8") as f:
                lines = f.read().splitlines()
            self.assertEqual(lines[:2], ["a#show version", "a|show version"])
            self.assertEqual(read_capture(stale),
                             {c: f"a|{c}" for c in parsers.COMMANDS["cisco_ios"]})
            if os.name == "posix":
                for name in os.listdir(raw):
                    mode = stat.S_IMODE(os.stat(os.path.join(raw, name)).st_mode)
                    self.assertEqual(mode, 0o600, name)

    def test_hostile_hostname_stays_in_the_directory(self):
        with tempfile.TemporaryDirectory() as d:
            path = devices.write_capture(d, "../../etc/passwd", {"show version": "x"})
            self.assertEqual(os.path.dirname(path), d)


class CliTest(unittest.TestCase):
    def run_cli(self, network, argv, env, netmiko=True):
        module = fake_netmiko(network) if netmiko else None
        err = io.StringIO()
        with contextlib.ExitStack() as stack:
            d = stack.enter_context(tempfile.TemporaryDirectory())
            stack.enter_context(mock.patch.dict(sys.modules, {"netmiko": module}))
            stack.enter_context(mock.patch.object(parsers, "parse_outputs", network.parse))
            stack.enter_context(mock.patch.dict(os.environ, env))
            stack.enter_context(contextlib.redirect_stderr(err))
            out = os.path.join(d, "topo.json")
            status = cli.main(["collect", "devices", *argv, "-o", out])
            text = open(out, encoding="utf-8").read() if os.path.exists(out) else ""
        return status, text, err.getvalue(), module.calls if module else []

    def test_collect_devices(self):
        network = chain_network(errors={"10.0.0.2": RuntimeError(f"echo {PASSWORD}")})
        status, text, err, calls = self.run_cli(
            network, ["--seed", "10.0.0.1", "--allow", "10.0.0.0/24", "--site", "HQ"],
            {"TOPOMAP_PASSWORD": PASSWORD, "TOPOMAP_USERNAME": "netops"})
        self.assertEqual(status, 0, err)
        topo = json.loads(text)
        self.assertEqual(topo["meta"]["collector"], "devices")
        self.assertEqual(topo["meta"]["contacted"], 2)
        self.assertIn("net:device:a", [n["id"] for n in topo["nodes"]])
        self.assertIn("10.0.0.2: RuntimeError: echo ***", err)
        self.assertNotIn(PASSWORD, text + err)
        connects = [kw for kind, kw in calls if kind == "connect"]
        self.assertEqual((connects[0]["username"], connects[0]["password"]), ("netops", PASSWORD))

    def test_missing_netmiko_and_bad_arguments(self):
        env = {"TOPOMAP_USERNAME": "netops"}
        status, _, err, _ = self.run_cli(chain_network(), ["--seed", "10.0.0.1"], env, netmiko=False)
        self.assertEqual(status, 2)
        self.assertIn("topomap[devices]", err)
        status, _, err, _ = self.run_cli(chain_network(), [], env)
        self.assertEqual(status, 2)
        self.assertIn("--seed", err)
        for argv in (["--allow", "10.0.0.0/33"], ["--platform", "linux"], ["--workers", "0"]):
            with self.assertRaises(SystemExit, msg=argv):
                self.run_cli(chain_network(), ["--seed", "10.0.0.1", *argv], env)


class RealParsersTest(unittest.TestCase):
    """End to end through the real parsers, with canned IOS output."""

    ADDRESSES = {"10.20.0.1": "core1.txt", "10.20.0.2": "dist1.txt"}

    def setUp(self):
        self.outputs = {a: read_capture(os.path.join(FIXTURES, f)) for a, f in self.ADDRESSES.items()}
        try:
            parsers.parse_outputs(self.outputs["10.20.0.1"], "cisco_ios", source="ssh://10.20.0.1")
        except NotImplementedError:
            self.skipTest("the parsers are not implemented yet")

    def test_crawl(self):
        outputs = self.outputs

        class Session:
            def __init__(self, host):
                self.host = host

            def send_command(self, command):
                return outputs[self.host][command]

            def disconnect(self):
                pass

        def connect(target, platform, login):
            if target.host not in outputs:
                raise NetmikoTimeoutException("TCP connection to device failed.")
            return Session(target.host)

        self.assertEqual(sorted(outputs["10.20.0.1"]), sorted(parsers.COMMANDS["cisco_ios"]))
        topo = crawl(["10.20.0.1"], LOGIN, allow=["10.20.0.0/16"], platform="cisco_ios",
                     connect=connect, detect=lambda target, login: "cisco_ios")
        self.assertEqual(collected(topo), ["core1", "dist1"])
        self.assertEqual(topo.nodes["net:device:dist1"].attrs["mgmt_ip"], "10.20.0.2")
        self.assertEqual(topo.nodes["net:device:isp-edge"].attrs["discovered"], "neighbor")
        self.assertTrue(any(l.kind == "physical" and {l.source, l.target} ==
                            {"net:device:core1", "net:device:dist1"} for l in topo.links.values()))
        # acc1 is allowed but unreachable; the phone and isp-edge are never tried.
        self.assertEqual(topo.meta["warnings"], ["10.20.0.3: timed out"])
        self.assertEqual((topo.meta["contacted"], topo.meta["failed"]), (3, 1))


if __name__ == "__main__":
    unittest.main()
