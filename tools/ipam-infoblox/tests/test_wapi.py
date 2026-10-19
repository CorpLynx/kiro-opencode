import ipaddress
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fake_wapi import PASSWORD, USER, VERSION, FakeGrid  # noqa: E402
from ipam_infoblox.wapi import Wapi, WapiError, load_existing  # noqa: E402


def client(grid, password=PASSWORD, version=VERSION):
    return Wapi(grid.url, USER, password, version, allow_http=True, timeout=10)


class WapiTest(unittest.TestCase):
    def test_reads_the_grid_page_by_page_with_one_login(self):
        with FakeGrid(page_size=1) as grid:
            wapi = client(grid)
            existing = load_existing(wapi, ["default", "aws-111122223333", "missing"])
        self.assertEqual(existing.views, ["aws-111122223333", "default"])
        default = existing.in_view("default")
        self.assertEqual(default[ipaddress.ip_network("10.0.0.0/8")]["kind"], "container")
        app = default[ipaddress.ip_network("10.0.2.0/24")]
        self.assertEqual((app["kind"], app["extattrs"]), ("network", {"AWS Account": "111122223333"}))
        self.assertEqual(list(existing.in_view("aws-111122223333")),
                         [ipaddress.ip_network("192.168.0.0/24")])
        self.assertEqual(existing.in_view("missing"), {})  # not in the Grid: not asked for
        self.assertEqual(existing.ea_defs["Environment"], {"type": "ENUM", "values": ["prod", "dev"]})
        # Paging followed (one object per page), with only GETs and one login.
        self.assertTrue(any("_page_id" in q for _, _, q in grid.requests))
        self.assertEqual({m for m, _, _ in grid.requests}, {"GET"})
        self.assertEqual(grid.logins, 1)
        self.assertFalse([q for _, p, q in grid.requests if "missing" in str(q)])

    def test_errors_are_explained(self):
        with FakeGrid() as grid:
            with self.assertRaisesRegex(WapiError, "refused the username or password"):
                client(grid, password="wrong").get("network")
            with self.assertRaisesRegex(WapiError, "HTTP 400: Unknown WAPI version"):
                client(grid, version="1.0").get("network")
            with self.assertRaisesRegex(WapiError, r"HTTP 400: Unknown object type \(gadget\)"):
                client(grid).get("gadget")

    def test_unreachable(self):
        with FakeGrid() as grid:
            url = grid.url
        with self.assertRaisesRegex(WapiError, "cannot reach"):
            Wapi(url, USER, PASSWORD, allow_http=True, timeout=2).get("network")

    def test_urls(self):
        self.assertEqual(Wapi("gm.example.net", USER, PASSWORD).base,
                         "https://gm.example.net/wapi/v2.12")
        self.assertEqual(Wapi("https://gm.example.net/ui/", USER, PASSWORD, "v2.13").base,
                         "https://gm.example.net/wapi/v2.13")
        with self.assertRaisesRegex(ValueError, "password in clear"):
            Wapi("http://gm.example.net", USER, PASSWORD)
        with self.assertRaisesRegex(ValueError, "not a Grid Manager URL"):
            Wapi("ftp://gm.example.net", USER, PASSWORD)

    def test_no_writes_are_possible(self):
        wapi = Wapi("gm.example.net", USER, PASSWORD)
        public = {n for n in dir(wapi) if not n.startswith("_")}
        self.assertEqual(public, {"all", "base", "calls", "get"})


if __name__ == "__main__":
    unittest.main()
