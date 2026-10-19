import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from topomapper.facts import (DeviceFacts, Interface, Neighbor, device_id,  # noqa: E402
                           facts_to_topology, guess_role, normalize_hostname, normalize_ifname)


class NormaliseTest(unittest.TestCase):
    def test_ifnames(self):
        cases = {"Gi1/0/1": "GigabitEthernet1/0/1", "Te1/1/1": "TenGigabitEthernet1/1/1",
                 "Eth1/49": "Ethernet1/49", "Po10": "Port-Channel10", "Fa0/1": "FastEthernet0/1",
                 "GigabitEthernet0/0": "GigabitEthernet0/0", "ge-0/0/0": "ge-0/0/0", "ae0": "ae0"}
        for raw, expected in cases.items():
            self.assertEqual(normalize_ifname(raw), expected, raw)

    def test_hostnames(self):
        self.assertEqual(normalize_hostname("Core1.example.net"), "core1")
        self.assertEqual(normalize_hostname("nx1(FDO12345678)"), "nx1")
        self.assertEqual(normalize_hostname("192.0.2.5"), "192.0.2.5")
        self.assertEqual(device_id("CORE1"), "net:device:core1")

    def test_roles(self):
        self.assertEqual(guess_role("ISR4451"), "router")
        self.assertEqual(guess_role("WS-C3850-48P"), "switch")
        self.assertEqual(guess_role("ASA5516"), "firewall")
        self.assertEqual(guess_role("", ["Router", "Switch"]), "router")
        self.assertEqual(guess_role("", [], "edge-fw1"), "firewall")
        self.assertEqual(guess_role("firmware box"), "unknown")


class FactsToTopologyTest(unittest.TestCase):
    def setUp(self):
        self.core = DeviceFacts(
            "core1", platform="cisco_ios", model="ISR4451", site="HQ",
            interfaces=[Interface("Gi0/0/0", ["10.0.0.1/30"]), Interface("Gi0/0/1", ["10.0.9.1/24"]),
                        Interface("Lo0", ["10.255.0.1/32"])],
            neighbors=[Neighbor("Gi0/0/0", "dist1.example.net", "GigabitEthernet1/0/48", protocol="cdp"),
                       Neighbor("Gi0/0/2", "ap7.example.net", "eth0", "10.0.5.7", "AIR-AP2802I", [], "cdp")])
        self.dist = DeviceFacts(
            "dist1", platform="cisco_ios", model="WS-C3850", site="HQ",
            interfaces=[Interface("GigabitEthernet1/0/48", ["10.0.0.2/30"]), Interface("Vlan9", ["10.0.9.2/24"])],
            neighbors=[Neighbor("Gi1/0/48", "core1", "Gi0/0/0", protocol="lldp")])
        self.fw = DeviceFacts("fw1", model="ASA5516", interfaces=[Interface("inside", ["10.0.9.3/24"]),
                                                                 Interface("outside", ["192.0.2.2/29"])])
        self.edge = DeviceFacts("edge1", interfaces=[Interface("Gi0/1", ["192.0.2.1/29"])])

    def test_build(self):
        t = facts_to_topology([self.core, self.dist, self.fw, self.edge])
        self.assertEqual(t.validate(), [])
        self.assertEqual(t.nodes["net:device:core1"].parent, "net:site:hq")
        self.assertEqual(t.nodes["net:device:ap7"].attrs["discovered"], "neighbor")
        self.assertEqual(t.nodes["net:device:ap7"].attrs["role"], "wireless")

        physical = [l for l in t.links.values() if l.kind == "physical"]
        self.assertEqual(len(physical), 2)  # the core-dist cable is seen twice but stored once
        cable = next(l for l in physical if "dist1" in l.target)
        self.assertEqual(cable.attrs["protocols"], ["cdp", "lldp"])
        self.assertEqual(cable.attrs["subnets"], ["10.0.0.0/30"])  # the /30 on it isn't a separate l3 link

        l3 = [l for l in t.links.values() if l.kind == "l3"]
        self.assertEqual([l.attrs["subnet"] for l in l3], ["192.0.2.0/29"])
        self.assertEqual(l3[0].attrs["addresses"], {"net:device:edge1": "192.0.2.1", "net:device:fw1": "192.0.2.2"})

        seg = t.nodes["net:segment:10.0.9.0/24"]
        members = [l for l in t.links.values() if l.kind == "segment-member"]
        self.assertEqual(seg.kind, "segment")
        self.assertEqual(len(members), 3)
        self.assertFalse(any("10.255.0.1" in str(l.attrs) for l in t.links.values()))  # loopbacks skipped

    def test_l3_exclude_and_off(self):
        t = facts_to_topology([self.core, self.dist, self.fw, self.edge], l3_exclude=["10.0.9.0/24"])
        self.assertNotIn("net:segment:10.0.9.0/24", t.nodes)
        t = facts_to_topology([self.fw, self.edge], infer_l3=False)
        self.assertEqual(t.links, {})


if __name__ == "__main__":
    unittest.main()
