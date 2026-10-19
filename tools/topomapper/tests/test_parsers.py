import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from topomapper import parsers  # noqa: E402
from topomapper.facts import DeviceFacts, Interface, Neighbor  # noqa: E402
from topomapper.parsers import (ParseError, canonical_command, detect_platform,  # noqa: E402
                             merge_facts, parse_config, parse_file, parse_outputs, parse_text,
                             split_capture)
from topomapper.parsers import arista_eos, cisco_ios, cisco_nxos, juniper_junos, tables  # noqa: E402
from topomapper.parsers.common import hostname_from_source, pick_port  # noqa: E402
from topomapper.parsers.config import junos_statements  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures", "devices")


def fixture(*parts):
    with open(os.path.join(FIXTURES, *parts), encoding="utf-8") as f:
        return f.read()


def sections(name):
    """{command: output} of a fixture capture, as the crawler would hand it
    over (the first section of each command)."""
    out = {}
    for s in split_capture(fixture(name)):
        out.setdefault(s.command, s.output)
    return out


def by_port(neighbors):
    return {(n.local_port, n.protocol): n for n in neighbors}


class CaptureTest(unittest.TestCase):
    def test_abbreviations(self):
        cases = {
            "sh ver": "show version", "sh cdp nei det": "show cdp neighbors detail",
            "sh lldp nei det": "show lldp neighbors detail", "sh lldp nei": "show lldp neighbors",
            "sh ip int": "show ip interface", "sh ipv6 int": "show ipv6 interface",
            "sh ip int br": "show ip interface brief", "sh run": "show running-config",
            "show ip int vrf all": "show ip interface vrf all", "sh int ter": "show interfaces terse",
            "show configuration | display set": "show configuration",
            "show lldp neighbors detail | no-more": "show lldp neighbors detail",
            "show lldp neighbors interface xe-0/1/0": "show lldp neighbors",
            "show running-config all": "show running-config", "sh chas hard": "show chassis hardware",
            "SHOW VERSION": "show version",
        }
        for typed, expected in cases.items():
            self.assertEqual(canonical_command(typed), expected, typed)
        self.assertEqual(parsers.KNOWN_COMMANDS[canonical_command("sh int desc")], "descriptions")
        for unknown in ("show clock", "terminal length 0", "", "sh i"):
            self.assertIsNone(canonical_command(unknown), unknown)

    def test_prompts(self):
        text = ("core1# terminal length 0\ncore1# show version\nver text\ncore1# terminal width 511\n"
                "core1#sh ipv6 int\n"
                "core1# show ip interface\nGi1 is up, line protocol is up\ncore1#\n")
        got = [(s.command, s.output, s.host) for s in split_capture(text)]
        # the first prompt must carry a show command; later ones (same host) need not
        self.assertEqual(got, [("show version", "ver text", "core1"),
                               ("terminal width 511", "", "core1"),
                               ("show ipv6 interface", "", "core1"),   # empty output section
                               ("show ip interface", "Gi1 is up, line protocol is up", "core1"),
                               ("", "", "core1")])

    def test_junos_prompts(self):
        for prompt in ("netops@edge1> ", "edge1#", "edge1> "):
            got = split_capture(f"{prompt}show version\nHostname: edge1\n{{master:0}}\n"
                                f"{prompt}show interfaces terse\nInterface Admin Link Proto\n")
            self.assertEqual([(s.command, s.host) for s in got],
                             [("show version", "edge1"), ("show interfaces terse", "edge1")], prompt)
            self.assertEqual(got[0].output, "Hostname: edge1")

    def test_banners(self):
        for banner in ("===== {} =====", "! {}", "### {}", "*** {} ***", "----- {} -----"):
            text = "\n".join([banner.format("show version"), "a", banner.format("sh lldp nei det"), "b"])
            got = [(s.command, s.output) for s in split_capture(text)]
            self.assertEqual(got, [("show version", "a"), ("show lldp neighbors detail", "b")], banner)
        got = split_capture("===== core2: show version =====\nx\n")
        self.assertEqual((got[0].host, got[0].command), ("core2", "show version"))

    def test_output_is_not_a_prompt(self):
        # config comments, NX-OS headers and Junos annotations are not section starts
        text = "!Command: show running-config\n! Last configuration change\n## Last commit: x\n"
        self.assertEqual(split_capture(text), [])

    def test_terminal_artefacts(self):
        text = "sw1#show version\r\nline one\r\n --More-- \x08\x08\x08\x08\x08\x08\x08\x08\x08\x08line two\r\n"
        self.assertEqual(split_capture(text)[0].output, "line one\nline two")


class DetectTest(unittest.TestCase):
    def test_fixtures(self):
        cases = {("edge1.txt",): "juniper_junos", ("core1.txt",): "cisco_nxos",
                 ("core2.log",): "cisco_nxos", ("dist1.txt",): "cisco_ios",
                 ("leaf1.txt",): "arista_eos", ("configs", "wan1-confg"): "cisco_ios",
                 ("configs", "edge1.conf"): "juniper_junos"}
        for parts, expected in cases.items():
            self.assertEqual(detect_platform(fixture(*parts)), expected, parts)

    def test_configs_and_outputs(self):
        self.assertEqual(detect_platform("set system host-name r1\n"), "juniper_junos")
        self.assertEqual(detect_platform("feature lldp\nswitchname n1\n"), "cisco_nxos")
        self.assertEqual(detect_platform("! device: leaf9 (DCS-7280SR-48C6, EOS-4.30.1F)\n"),
                         "arista_eos")
        self.assertEqual(detect_platform("hello world"), None)

    def test_neighbor_text_does_not_mislead(self):
        # dist1's CDP/LLDP output quotes NX-OS and Junos; only its own output counts
        self.assertEqual(detect_platform(sections("dist1.txt")["show cdp neighbors detail"]), "cisco_ios")


class VersionTest(unittest.TestCase):
    def test_ios_xe(self):
        v = cisco_ios.parse_version(sections("dist1.txt")["show version"])
        self.assertEqual(v, {"hostname": "dist1", "os_version": "17.09.04a", "model": "C9300-48P",
                             "serial": "FOC2345X0AB"})

    def test_classic_ios(self):
        text = ("Cisco IOS Software, C2900 Software (C2900-UNIVERSALK9-M), Version 15.7(3)M8, "
                "RELEASE SOFTWARE (fc1)\nbr1 uptime is 2 years, 1 week\n"
                "Cisco CISCO2911/K9 (revision 1.0) with 491520K/32768K bytes of memory.\n"
                "Processor board ID FTX0000A0AA\n")
        self.assertEqual(cisco_ios.parse_version(text),
                         {"hostname": "br1", "os_version": "15.7(3)M8", "model": "CISCO2911/K9",
                          "serial": "FTX0000A0AA"})

    def test_nxos(self):
        v = cisco_nxos.parse_version(sections("core1.txt")["show version"])
        self.assertEqual(v, {"hostname": "core1", "os_version": "9.3(10)",
                             "model": "Nexus9000 C93180YC-FX", "serial": "FDO23456789"})

    def test_eos(self):
        s = sections("leaf1.txt")
        self.assertEqual(arista_eos.parse_version(s["show version"]),
                         {"model": "DCS-7050SX3-48YC8-F", "serial": "JPE21345678", "os_version": "4.28.3M"})
        self.assertEqual(arista_eos.parse_hostname(s["show hostname"]), {"hostname": "leaf1"})

    def test_junos(self):
        s = sections("edge1.txt")
        self.assertEqual(juniper_junos.parse_version(s["show version"]),
                         {"hostname": "edge1", "model": "mx204", "os_version": "21.4R3-S5.4"})
        self.assertEqual(juniper_junos.parse_chassis_hardware(s["show chassis hardware"])["serial"],
                         "DX0000AF0042")
        old = "Hostname: r1\nModel: srx345\nJUNOS Software Release [15.1X49-D200.3]\n"
        self.assertEqual(juniper_junos.parse_version(old)["os_version"], "15.1X49-D200.3")


class NeighborTest(unittest.TestCase):
    def test_ios_cdp(self):
        n = by_port(cisco_ios.parse_cdp_detail(sections("dist1.txt")["show cdp neighbors detail"]))
        core = n[("TenGigabitEthernet1/1/1", "cdp")]
        self.assertEqual((core.remote_name, core.remote_port, core.remote_mgmt_ip, core.remote_platform),
                         ("core1.example.net(FDO23456789)", "Ethernet1/1", "192.168.0.11", "N9K-C93180YC-FX"))
        self.assertEqual(core.remote_capabilities, ["router", "switch"])
        self.assertEqual(n[("GigabitEthernet1/0/48", "cdp")].remote_platform, "ISR4331/K9")
        phone = n[("GigabitEthernet1/0/10", "cdp")]
        self.assertEqual((phone.remote_port, phone.remote_platform, phone.remote_mgmt_ip),
                         ("Port 1", "Cisco IP Phone 8841", "10.20.110.25"))  # entry address when no mgmt
        self.assertEqual(phone.remote_capabilities, ["station", "phone"])
        self.assertEqual(n[("GigabitEthernet1/0/1", "cdp")].remote_capabilities, ["bridge"])

    def test_nxos_cdp(self):
        n = by_port(cisco_ios.parse_cdp_detail(sections("core1.txt")["show cdp neighbors detail"]))
        self.assertEqual(sorted(p for p, _ in n), ["Ethernet1/1", "Ethernet1/53"])
        dist = n[("Ethernet1/1", "cdp")]
        self.assertEqual((dist.remote_name, dist.remote_port, dist.remote_mgmt_ip, dist.remote_platform),
                         ("dist1.example.net", "TenGigabitEthernet1/1/1", "192.168.0.13", "C9300-48P"))

    def test_ios_lldp(self):
        n = by_port(cisco_ios.parse_lldp_detail(sections("dist1.txt")["show lldp neighbors detail"]))
        self.assertEqual(len(n), 4)
        core = n[("Te1/1/1", "lldp")]
        self.assertEqual((core.remote_name, core.remote_port, core.remote_mgmt_ip),
                         ("core1.example.net", "Ethernet1/1", "192.168.0.11"))
        self.assertTrue(core.remote_platform.startswith("Cisco Nexus Operating System"))
        self.assertEqual(n[("Gi1/0/1", "lldp")].remote_capabilities, ["bridge", "wlan"])
        phone = n[("Gi1/0/10", "lldp")]
        self.assertEqual((phone.remote_name, phone.remote_port), ("SEP00005E0053A0", "00005E0053A0:P1"))
        self.assertEqual(phone.remote_capabilities, ["bridge", "phone"])

    def test_nxos_lldp(self):
        n = by_port(cisco_nxos.parse_lldp_detail(sections("core1.txt")["show lldp neighbors detail"]))
        self.assertEqual(sorted(p for p, _ in n), ["Eth1/1", "Eth1/2", "Eth1/49", "Eth1/53"])
        edge = n[("Eth1/49", "lldp")]
        self.assertEqual((edge.remote_name, edge.remote_port, edge.remote_mgmt_ip, edge.remote_capabilities),
                         ("edge1.example.net", "xe-0/1/0", "192.168.0.1", ["bridge", "router"]))

    def test_eos_lldp(self):
        n = by_port(arista_eos.parse_lldp_detail(sections("leaf1.txt")["show lldp neighbors detail"]))
        self.assertEqual(sorted(p for p, _ in n), ["Ethernet1", "Ethernet49/1", "Ethernet50/1"])
        core = n[("Ethernet49/1", "lldp")]
        self.assertEqual((core.remote_name, core.remote_port, core.remote_mgmt_ip),
                         ("core1.example.net", "Ethernet1/2", "192.168.0.11"))
        self.assertEqual(core.remote_platform, "Cisco Nexus Operating System (NX-OS) Software 9.3(10)")
        srv = n[("Ethernet1", "lldp")]   # MAC port ID: the description names the port
        self.assertEqual((srv.remote_name, srv.remote_port, srv.remote_capabilities),
                         ("srv1.example.net", "eth0", ["station"]))

    def test_junos_lldp(self):
        # the capture has the summary and two `show lldp neighbors interface X` sections
        summary, *details = [x.output for x in split_capture(fixture("edge1.txt"))
                             if x.command == "show lldp neighbors"]
        n = juniper_junos.parse_lldp(details[0])[0]
        self.assertEqual((n.local_port, n.remote_name, n.remote_port, n.remote_mgmt_ip, n.remote_capabilities),
                         ("xe-0/1/0", "core1.example.net", "Ethernet1/49", "192.168.0.11",
                          ["bridge", "router"]))
        self.assertEqual([(n.local_port, n.remote_name, n.remote_port, n.remote_mgmt_ip)
                          for n in juniper_junos.parse_lldp(summary)],
                         [("xe-0/1/0", "core1.example.net", "Ethernet1/49", None),
                          ("xe-0/1/1", "core2.example.net", "Ethernet1/49", None)])

    def test_junos_lldp_units_and_old_summary(self):
        text = ("Local Interface    Chassis Id          Port info     System Name\n"
                "ge-0/0/0.0         00:00:5e:00:53:01   ge-0/0/1.0    sw2\n")
        n = juniper_junos.parse_lldp(text)[0]
        self.assertEqual((n.local_port, n.remote_port, n.remote_name), ("ge-0/0/0", "ge-0/0/1", "sw2"))

    def test_port_choice(self):
        self.assertEqual(pick_port("513", "ge-0/0/0"), "ge-0/0/0")
        self.assertEqual(pick_port("Gi1/0/1", "uplink to core"), "Gi1/0/1")
        self.assertEqual(pick_port("0000.5e00.5301", "SW PORT"), "0000.5e00.5301")


class InterfaceTest(unittest.TestCase):
    def test_ios_ip_interface(self):
        got = {i.name: i for i in cisco_ios.parse_ip_interface(sections("dist1.txt")["show ip interface"])}
        self.assertEqual(got["Vlan100"].addresses, ["10.20.100.4/24"])
        self.assertEqual((got["GigabitEthernet0/0"].vrf, got["GigabitEthernet0/0"].status), ("Mgmt-vrf", "up"))
        self.assertEqual(got["Vlan1"].status, "admin-down")
        self.assertEqual(got["TenGigabitEthernet1/1/1"].addresses, [])
        text = ("Gi0/1 is up, line protocol is down\n  Internet address is 10.1.1.1/24\n"
                "  Secondary address 10.1.2.1/24\n")
        i = cisco_ios.parse_ip_interface(text)[0]
        self.assertEqual((i.addresses, i.status), (["10.1.1.1/24", "10.1.2.1/24"], "down"))

    def test_ios_ipv6(self):
        i = cisco_ios.parse_ip_interface(sections("dist1.txt")["show ipv6 interface"])[0]
        self.assertEqual((i.name, i.addresses), ("Vlan100", ["2001:db8:20:100::4/64"]))

    def test_eos_ip_interface(self):
        s = sections("leaf1.txt")
        got = {i.name: i for i in cisco_ios.parse_ip_interface(s["show ip interface"])}
        self.assertEqual((got["Management1"].vrf, got["Management1"].description), ("MGMT", "oob-mgmt"))
        self.assertEqual(got["Ethernet49/1"].addresses, ["10.0.0.9/31"])
        v6 = cisco_ios.parse_ip_interface(s["show ipv6 interface"])
        self.assertEqual(v6[0].addresses, ["2001:db8:0:3::2/64"])

    def test_nxos_ip_interface(self):
        s = sections("core1.txt")
        got = {i.name: i for i in cisco_nxos.parse_ip_interface(s["show ip interface vrf all"])}
        self.assertEqual((got["mgmt0"].addresses, got["mgmt0"].vrf), (["192.168.0.11/24"], "management"))
        self.assertEqual((got["Ethernet1/49"].addresses, got["Ethernet1/49"].vrf), (["10.0.0.1/31"], ""))
        self.assertEqual(got["Ethernet1/54"].status, "admin-down")
        v6 = {i.name: i for i in cisco_nxos.parse_ip_interface(s["show ipv6 interface vrf all"])}
        self.assertEqual(v6["Ethernet1/49"].addresses, ["2001:db8:0:1::2/64"])  # no link-local, no subnet

    def test_junos_terse(self):
        got = {i.name: i for i in juniper_junos.parse_terse(sections("edge1.txt")["show interfaces terse"])}
        self.assertEqual(got["xe-0/1/0"].addresses, ["10.0.0.0/31", "2001:db8:0:1::1/64"])
        self.assertEqual(got["lo0"].addresses, ["10.255.255.1/32"])
        self.assertEqual(got["fxp0"].addresses, ["192.168.0.1/24"])
        self.assertEqual(got["xe-0/1/2"].status, "down")
        for internal in ("em1", "jsrv", "lo0.16384", "lsi", "cbp0", "lo0.0", "xe-0/1/0.0"):
            self.assertNotIn(internal, got)

    def test_descriptions(self):
        ios = {i.name: i for i in tables.parse_descriptions(sections("dist1.txt")["show interfaces description"])}
        self.assertEqual((ios["Gi1/0/2"].description, ios["Gi1/0/2"].status), ("spare", "admin-down"))
        self.assertEqual((ios["Te1/1/1"].description, ios["Te1/1/1"].status), ("to core1 Eth1/1", "up"))
        nx = {i.name: i for i in tables.parse_descriptions(sections("core1.txt")["show interface description"])}
        self.assertEqual((nx["Eth1/2"].description, nx["Vlan999"].description, nx["Eth1/3"].description),
                         ("to leaf1 Et49/1", "ospf transit to core2", ""))
        jn = {i.name: i for i in tables.parse_descriptions(sections("edge1.txt")["show interfaces descriptions"])}
        self.assertEqual(jn["gr-0/0/0"].description, "GRE to wan1")   # unit 0 folded
        self.assertEqual(jn["xe-0/1/7"].status, "up")

    def test_ip_brief(self):
        ios = {i.name: i for i in tables.parse_ip_brief(sections("dist1.txt")["show ip interface brief"])}
        self.assertEqual(ios["Vlan100"].addresses, [])   # no mask in the output: not used
        self.assertEqual((ios["Vlan100"].status, ios["Vlan1"].status), ("up", "admin-down"))
        eos = tables.parse_ip_brief(
            "Interface         IP Address           Status       Protocol         MTU    Owner\n"
            "----------------- -------------------- ------------ -------------- ---------- -------\n"
            "Ethernet49/1      10.0.0.9/31          up           up              9214\n")
        self.assertEqual((eos[0].addresses, eos[0].status), (["10.0.0.9/31"], "up"))
        nx = tables.parse_ip_brief('IP Interface Status for VRF "management"(2)\n'
                                   "Interface            IP Address      Interface Status\n"
                                   "mgmt0                192.168.0.11    protocol-up/link-up/admin-up\n")
        self.assertEqual((nx[0].vrf, nx[0].status, nx[0].addresses), ("management", "up", []))

    def test_brief_masks_come_from_elsewhere(self):
        facts = parse_outputs({"show ip interface brief": sections("dist1.txt")["show ip interface brief"]},
                              "cisco_ios", hostname="dist1")
        self.assertTrue(all(not i.addresses for i in facts.interfaces))
        facts = parse_text(fixture("dist1.txt"))
        vlan = next(i for i in facts.interfaces if i.name == "Vlan100")
        self.assertEqual(vlan.addresses, ["10.20.100.4/24", "2001:db8:20:100::4/64"])


IOS_CONFIG = """\
hostname dist9
!
interface GigabitEthernet0/0
 vrf forwarding Mgmt-vrf
 ip address 192.168.0.99 255.255.255.0
!
interface GigabitEthernet1/0/48
 description to wan9 Gi0/0/1
 no switchport
 ip address 10.9.0.1 255.255.255.252
 ip address 10.9.0.5 255.255.255.252 secondary
 ipv6 address 2001:DB8:9::1/64
 ipv6 address FE80::1 link-local
 shutdown
!
interface Vlan10
 ip vrf forwarding USERS
 ip address dhcp
!
router ospf 1
 network 10.9.0.0 0.0.0.3 area 0
"""

NXOS_CONFIG = """\
!Command: show running-config
!Running configuration last done at: Fri Oct 16 02:00:00 2026
version 9.3(10) Bios:version 05.45
switchname core9
feature lldp
interface mgmt0
  vrf member management
  ip address 192.168.0.98/24
interface Ethernet1/1
  description uplink
  no switchport
  ip address 10.9.1.1/31
  no shutdown
"""

EOS_CONFIG = """\
! device: leaf9 (DCS-7050SX3-48YC8, EOS-4.28.3M)
hostname leaf9
interface Management1
   vrf MGMT
   ip address 192.168.0.97/24
interface Ethernet1
   no switchport
   ip address 10.9.2.1/31
"""

JUNOS_SET = """\
set version 21.4R3-S5.4
set system host-name edge9
set interfaces xe-0/0/0 description "to core9 Eth1/1"
set interfaces xe-0/0/0 unit 0 family inet address 10.9.1.0/31
set interfaces xe-0/0/0 unit 0 family inet6 address 2001:db8:9:1::1/64
set interfaces xe-0/0/1 disable
set interfaces xe-0/0/1 vlan-tagging
set interfaces xe-0/0/1 unit 100 vlan-id 100
set interfaces xe-0/0/1 unit 100 family inet address 10.9.100.1/24
set interfaces xe-0/0/2 unit 0 family inet address 10.9.3.1/30
deactivate interfaces xe-0/0/2
set interfaces fxp0 unit 0 family inet address 192.168.0.96/24
set routing-instances CUST interface xe-0/0/1.100
"""

JUNOS_CURLY = """\
## Last commit: 2026-10-12 14:03:21 UTC by netops
version 21.4R3-S5.4;
system {
    host-name edge9;
    root-authentication {
        encrypted-password "$6$x$y"; ## SECRET-DATA
    }
}
interfaces {
    xe-0/0/0 {
        description "to core9 Eth1/1";
        unit 0 {
            family inet {
                address 10.9.1.0/31;
            }
            family inet6 {
                address 2001:db8:9:1::1/64;
            }
        }
    }
    xe-0/0/1 {
        disable;
        vlan-tagging;
        unit 100 {
            vlan-id 100;
            family inet {
                address 10.9.100.1/24 {
                    primary;
                }
            }
        }
    }
    /* decommissioned */
    inactive: xe-0/0/2 {
        unit 0 {
            family inet {
                address 10.9.3.1/30;
            }
        }
    }
    fxp0 {
        unit 0 {
            family inet {
                address 192.168.0.96/24;
            }
        }
    }
}
routing-instances {
    CUST {
        interface xe-0/0/1.100;
    }
}
"""


class ConfigTest(unittest.TestCase):
    def test_ios(self):
        f = parse_config(IOS_CONFIG)
        got = {i.name: i for i in f.interfaces}
        self.assertEqual(f.hostname, "dist9")
        self.assertEqual(got["GigabitEthernet1/0/48"].addresses,
                         ["10.9.0.1/30", "10.9.0.5/30", "2001:db8:9::1/64"])
        self.assertEqual((got["GigabitEthernet1/0/48"].status, got["GigabitEthernet1/0/48"].description),
                         ("admin-down", "to wan9 Gi0/0/1"))
        self.assertEqual((got["Vlan10"].vrf, got["Vlan10"].addresses), ("USERS", []))
        self.assertEqual(f.mgmt_ip, "192.168.0.99")   # via the management VRF

    def test_nxos_and_eos(self):
        nx = parse_config(NXOS_CONFIG)
        self.assertEqual((nx.hostname, nx.mgmt_ip), ("core9", "192.168.0.98"))
        self.assertEqual({i.name: i.vrf for i in nx.interfaces}["mgmt0"], "management")
        eos = parse_config(EOS_CONFIG)
        got = {i.name: i for i in eos.interfaces}
        self.assertEqual((eos.hostname, got["Management1"].vrf, got["Ethernet1"].addresses),
                         ("leaf9", "MGMT", ["10.9.2.1/31"]))

    def test_junos_both_styles(self):
        for text in (JUNOS_SET, JUNOS_CURLY):
            f = parse_config(text)
            got = {i.name: i for i in f.interfaces}
            self.assertEqual(f.hostname, "edge9")
            self.assertEqual(got["xe-0/0/0"].addresses, ["10.9.1.0/31", "2001:db8:9:1::1/64"])
            self.assertEqual(got["xe-0/0/0"].description, "to core9 Eth1/1")
            self.assertEqual(got["xe-0/0/1"].status, "admin-down")
            self.assertEqual((got["xe-0/0/1.100"].addresses, got["xe-0/0/1.100"].vrf),
                             (["10.9.100.1/24"], "CUST"))
            self.assertNotIn("xe-0/0/2", got)   # inactive / deactivated
            self.assertEqual(f.mgmt_ip, "192.168.0.96")

    def test_junos_statements(self):
        self.assertEqual(junos_statements(JUNOS_CURLY)[:2],
                         [["version", "21.4R3-S5.4"], ["system", "host-name", "edge9"]])
        set_paths = {tuple(s) for s in junos_statements(JUNOS_SET)}
        self.assertIn(("interfaces", "xe-0/0/0", "description", "to core9 Eth1/1"), set_paths)

    def test_config_fixtures(self):
        edge = parse_text(fixture("configs", "edge1.conf"))
        got = {i.name: i for i in edge.interfaces}
        self.assertEqual(edge.hostname, "edge1")
        self.assertEqual(got["xe-0/1/7"].vrf, "INTERNET")
        self.assertEqual(got["gr-0/0/0"].description, "GRE to wan1")
        self.assertNotIn("xe-0/1/6", got)
        wan = parse_text(fixture("configs", "wan1-confg"))
        self.assertEqual((wan.hostname, wan.platform, wan.vendor, wan.mgmt_ip),
                         ("wan1", "cisco_ios", "cisco", "192.168.0.31"))
        self.assertEqual({i.name: i.addresses for i in wan.interfaces}["Tunnel0"], ["172.16.0.2/30"])


class ParseOutputsTest(unittest.TestCase):
    """The crawler's entry point."""

    def test_every_platform(self):
        cases = {"edge1.txt": ("juniper_junos", "edge1", "192.168.0.1", 2),
                 "core1.txt": ("cisco_nxos", "core1", "192.168.0.11", 6),
                 "dist1.txt": ("cisco_ios", "dist1", "192.168.0.13", 9),
                 "leaf1.txt": ("arista_eos", "leaf1", "192.168.0.21", 3)}
        for name, (platform, hostname, mgmt, neighbors) in cases.items():
            outputs = sections(name)
            for command in parsers.COMMANDS[platform]:
                self.assertIn(command, outputs, (name, command))   # fixtures cover the crawl commands
            outputs = {c: outputs[c] for c in parsers.COMMANDS[platform]}
            facts = parse_outputs(outputs, None, source=f"ssh://{mgmt}")
            self.assertEqual((facts.platform, facts.hostname, facts.mgmt_ip), (platform, hostname, mgmt), name)
            self.assertEqual(len(facts.neighbors), neighbors, name)
            self.assertTrue(facts.model and facts.os_version and facts.serial, name)

    def test_ios_mgmt_addresses_from_cdp(self):
        facts = parse_outputs({"show cdp neighbors detail": sections("dist1.txt")["show cdp neighbors detail"]},
                              "cisco_ios", hostname="dist1")
        self.assertEqual({n.remote_name: n.remote_mgmt_ip for n in facts.neighbors}["wan1.example.net"],
                         "10.0.0.14")

    def test_errors_and_unknowns_are_skipped(self):
        facts = parse_outputs({"show version": sections("dist1.txt")["show version"],
                               "show lldp neighbors detail": "% LLDP is not enabled",
                               "show ipv6 interface": "",
                               "show cdp neighbors detail": "                 ^\n% Invalid input detected at '^' marker.",
                               "show clock": "12:00:00.000 UTC Sat Oct 17 2026"}, "cisco_ios")
        self.assertEqual((facts.hostname, facts.neighbors, facts.interfaces), ("dist1", [], []))

    def test_hostname_fallbacks(self):
        self.assertEqual(parse_outputs({}, "cisco_ios", "ssh://192.0.2.7").hostname, "192.0.2.7")
        self.assertEqual(parse_outputs({}, "cisco_ios", hostname="sw7").hostname, "sw7")
        self.assertEqual(hostname_from_source("backups/core1-confg.txt"), "core1")
        self.assertEqual(hostname_from_source("core1_2026-10-16.log"), "core1")

    def test_cdp_and_lldp_ports_reconciled(self):
        facts = parse_text(fixture("dist1.txt"))
        phone = [n for n in facts.neighbors if n.remote_name == "SEP00005E0053A0"]
        self.assertEqual(sorted((n.protocol, n.remote_port) for n in phone),
                         [("cdp", "Port 1"), ("lldp", "Port 1")])


class ParseTextTest(unittest.TestCase):
    def test_capture_with_config_section(self):
        text = "leaf9#show running-config\n" + EOS_CONFIG + "leaf9#show hostname\nHostname: leaf9\n"
        f = parse_text(text)
        self.assertEqual((f.hostname, f.platform, f.mgmt_ip), ("leaf9", "arista_eos", "192.168.0.97"))

    def test_junos_hash_prompt_and_empty_section(self):
        text = ("edge9#show version\nHostname: edge9\nModel: mx204\nJunos: 21.4R3-S5.4\n"
                "edge9#show lldp neighbors\nedge9#show interfaces terse\n"
                "Interface               Admin Link Proto    Local                 Remote\n"
                "xe-0/0/0                up    up\n"
                "xe-0/0/0.0              up    up   inet     10.9.1.0/31\n")
        f = parse_text(text)
        self.assertEqual((f.hostname, f.platform, f.model), ("edge9", "juniper_junos", "mx204"))
        self.assertEqual([(i.name, i.addresses) for i in f.interfaces], [("xe-0/0/0", ["10.9.1.0/31"])])

    def test_hostname_from_prompt_and_source(self):
        f = parse_text("sw7#show ip interface\nVlan5 is up, line protocol is up\n"
                       "  Internet address is 10.5.0.1/24\n")
        self.assertEqual(f.hostname, "sw7")
        f = parse_text("Vlan5 is up, line protocol is up\n  Internet address is 10.5.0.1/24\n",
                       "cisco_ios", "saved/sw8_show.txt")
        self.assertEqual((f.hostname, f.interfaces[0].addresses), ("sw8", ["10.5.0.1/24"]))

    def test_several_devices_in_one_file(self):
        text = ("sw1#show version\nsw1 uptime is 1 week\nsw1#exit\n"
                "sw2#show version\nsw2 uptime is 2 weeks\n")
        self.assertEqual([f.hostname for f in parse_file(text)], ["sw1", "sw2"])
        self.assertEqual(parse_text(text).hostname, "sw1")

    def test_unrecognisable(self):
        with self.assertRaises(ParseError):
            parse_text("just some notes\nnothing to see\n")


class MergeTest(unittest.TestCase):
    def test_merge(self):
        a = DeviceFacts("edge1", model="mx204", source="a.txt",
                        interfaces=[Interface("xe-0/1/7", ["203.0.113.2/30"], status="up")],
                        neighbors=[Neighbor("xe-0/1/0", "core1.example.net", "Ethernet1/49", protocol="lldp")])
        b = DeviceFacts("EDGE1.example.net", serial="X1", source="b.conf",
                        interfaces=[Interface("xe-0/1/7", ["203.0.113.2/30"], "ISP", vrf="INTERNET"),
                                    Interface("fxp0", ["192.168.0.1/24"])],
                        neighbors=[Neighbor("xe-0/1/0", "core1", "Eth1/49", "192.168.0.11", protocol="lldp")])
        other = DeviceFacts("core1", source="c.txt")
        merged = merge_facts([a, other, b])
        self.assertEqual([f.hostname for f in merged], ["edge1", "core1"])
        edge = merged[0]
        self.assertEqual((edge.model, edge.serial, edge.mgmt_ip, edge.source),
                         ("mx204", "X1", "192.168.0.1", "a.txt; b.conf"))
        self.assertEqual([(i.name, i.addresses, i.description, i.status, i.vrf) for i in edge.interfaces],
                         [("xe-0/1/7", ["203.0.113.2/30"], "ISP", "up", "INTERNET"),
                          ("fxp0", ["192.168.0.1/24"], "", "", "")])
        self.assertEqual(len(edge.neighbors), 1)
        self.assertEqual(edge.neighbors[0].remote_mgmt_ip, "192.168.0.11")

    def test_capture_and_config(self):
        merged = merge_facts([parse_text(fixture("edge1.txt"), source="edge1.txt"),
                              parse_text(fixture("configs", "edge1.conf"), source="edge1.conf")])
        self.assertEqual(len(merged), 1)
        got = {i.name: i for i in merged[0].interfaces}
        self.assertEqual((got["xe-0/1/7"].vrf, got["xe-0/1/7"].status), ("INTERNET", "up"))
        self.assertEqual(merged[0].serial, "DX0000AF0042")


if __name__ == "__main__":
    unittest.main()
