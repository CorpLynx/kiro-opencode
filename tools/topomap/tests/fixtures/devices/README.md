# Fixture lab

Made-up saved output for a small lab. The devices reference each other consistently, so collecting the whole directory gives one connected topology across vendors.

```
topomap collect files tests/fixtures/devices --site-map tests/fixtures/devices/sites.csv --l3-exclude 192.168.0.0/24
```

All addresses are RFC 1918 or documentation ranges. Names are under `example.net`, and serial numbers and MACs (`00:00:5e:00:53:xx`) are invented.

## Devices

| Device | Platform | Files | Capture style |
| --- | --- | --- | --- |
| edge1 | Juniper MX204, Junos 21.4R3-S5.4 | `edge1.txt`, `configs/edge1.conf` (curly-brace) | `netops@edge1> show ...` prompts. Has the LLDP summary and per-interface detail. |
| core1 | Cisco Nexus 93180YC-FX, NX-OS 9.3(10) | `core1.txt` | `core1# show ...` prompts |
| core2 | Cisco Nexus 93180YC-FX, NX-OS 9.3(10) | `core2.log` | `===== show ... =====` banners. No prompts, so the hostname comes from `show version`. |
| dist1 | Cisco Catalyst C9300-48P, IOS-XE 17.9.4a | `dist1.txt` | `dist1#sh cdp nei det` (abbreviated commands). Includes `sh ip int br`. |
| leaf1 | Arista DCS-7050SX3-48YC8-F, EOS 4.28.3M | `leaf1.txt` | `leaf1#show ...` prompts |
| wan1 | Cisco ISR4331, IOS-XE 17.6 | `configs/wan1-confg` | Config only |

Every capture contains the output of each command in `parsers.COMMANDS` for its platform.

Not collected, seen only as neighbours:

- **ap1:** an AIR-AP2802I, seen over CDP and LLDP.
- **SEP00005E0053A0:** an IP phone 8841, seen over CDP and LLDP.
- **srv1.example.net:** a Linux server, seen over LLDP.

## Cables

| A | B | Seen by | Subnet |
| --- | --- | --- | --- |
| edge1 xe-0/1/0 | core1 Ethernet1/49 | LLDP | 10.0.0.0/31, 2001:db8:0:1::/64 |
| edge1 xe-0/1/1 | core2 Ethernet1/49 | LLDP | 10.0.0.2/31, 2001:db8:0:2::/64 |
| core1 Ethernet1/53 | core2 Ethernet1/53 | CDP, LLDP | 10.0.0.4/31 |
| core1 Ethernet1/1 | dist1 TenGigabitEthernet1/1/1 | CDP, LLDP | trunk |
| core2 Ethernet1/1 | dist1 TenGigabitEthernet1/1/2 | CDP, LLDP | trunk |
| core1 Ethernet1/2 | leaf1 Ethernet49/1 | LLDP | 10.0.0.8/31, 2001:db8:0:3::/64 |
| core2 Ethernet1/2 | leaf1 Ethernet50/1 | LLDP | 10.0.0.10/31 |
| dist1 GigabitEthernet1/0/48 | wan1 GigabitEthernet0/0/1 | CDP (dist1 only) | 10.0.0.12/30 |
| dist1 GigabitEthernet1/0/1 | ap1 GigabitEthernet0 | CDP, LLDP | access VLAN 100 |
| dist1 GigabitEthernet1/0/10 | SEP00005E0053A0 Port 1 | CDP, LLDP | VLANs 100/110 |
| leaf1 Ethernet1 | srv1 eth0 | LLDP | access VLAN 200 |

The ports are reported in each vendor's own style: `Eth1/49`, `Te1/1/1`, `Et49/1`, `xe-0/1/0`. NX-OS CDP IDs carry a `(serial)` suffix.

## Layer 3 without a cable

- **core1 Vlan999 ↔ core2 Vlan999:** 10.0.0.20/30, an OSPF transit VLAN.
- **edge1 gr-0/0/0 ↔ wan1 Tunnel0:** 172.16.0.0/30, a GRE tunnel.
- **10.20.100.0/24 and 2001:db8:20:100::/64:** VLAN 100, shared by core1, core2 and dist1. These become segments.
- **192.168.0.0/24:** the out-of-band management network (mgmt0, fxp0, Management1, Gi0/0 in Mgmt-vrf, Gi0 in Mgmt-intf). All six devices share it, so it becomes a segment unless you exclude it with `--l3-exclude`.

edge1's ISP link (203.0.113.0/30, in routing instance `INTERNET`) and wan1's MPLS link (198.51.100.0/30) have no peer in the lab.

## Other files

`sites.csv` maps hostnames to sites for `--site-map`, using a `core*` wildcard row. The collector skips `.md` and `.csv` files when it walks a directory.
