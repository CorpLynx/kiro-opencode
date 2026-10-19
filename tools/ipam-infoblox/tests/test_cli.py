import contextlib
import io
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fake_wapi import PASSWORD, USER, FakeGrid  # noqa: E402
from ipam_infoblox.cli import main  # noqa: E402

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "ipam-export.csv")
CREDENTIALS = {"INFOBLOX_USERNAME": USER, "INFOBLOX_PASSWORD": PASSWORD}


def run(*argv, env=None):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
            mock.patch.dict(os.environ, env or {}):
        status = main(list(argv))
    return status, out.getvalue(), err.getvalue()


class CliTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def path(self, name):
        return os.path.join(self.dir.name, name)

    def read(self, name):
        with open(self.path(name), encoding="utf-8") as handle:
            return handle.read()

    def test_inspect(self):
        status, out, _ = run("inspect", FIXTURE)
        self.assertEqual(status, 0)
        self.assertIn("header on row 3, 12 data rows", out)
        self.assertIn("cidr               Resource CIDR", out)
        self.assertIn("other columns (usable as column:HEADER): IP usage", out)
        self.assertIn("ranges by type: pool 2, subnet 5, vpc 4", out)
        self.assertIn("row 12: eip is an address, not a network", out)

    def test_plan_offline(self):
        status, _, err = run("plan", FIXTURE, "-o", self.path("import.csv"),
                             "--report", self.path("report.md"))
        self.assertEqual(status, 0, err)
        self.assertIn("6 container(s) and 5 network(s) to import", err)
        self.assertIn("not checked against Infoblox", err)
        text = self.read("import.csv")
        self.assertTrue(text.startswith("header-networkcontainer,address*,netmask*,"))
        self.assertIn("network,10.1.1.0,255.255.255.0,default,dev-a (subnet-0a1b2c3d4e5f60003)", text)
        for name in ("import.csv", "report.md"):
            self.assertEqual(stat.S_IMODE(os.stat(self.path(name)).st_mode), 0o600)
        report = self.read("report.md")
        self.assertIn("| 11 | 6 | 5 | 0 | 0 | 2 | 2 |", report)
        self.assertIn("| 10 | host bits cleared: now 10.1.1.0/24 |", report)

    def test_plan_against_the_grid(self):
        with FakeGrid() as grid:
            status, _, err = run("plan", FIXTURE, "-o", self.path("import.csv"),
                                 "--report", self.path("report.md"), "--wapi", grid.url,
                                 "--allow-http", "--ea", "AWS Account=account", env=CREDENTIALS)
        self.assertEqual(status, 1, err)  # 10.3.5.0/24 sits inside the Grid's network 10.3.0.0/16
        self.assertIn("2 already in Infoblox, 1 conflict(s) left out", err)
        text = self.read("import.csv")
        self.assertNotIn("10.3.5.0", text)
        self.assertNotIn("networkcontainer,10.0.0.0,8,", text)     # the Grid has it
        self.assertNotIn("network,10.0.2.0,", text)                # and this one
        self.assertIn("network,10.0.1.0,255.255.255.0,default,prod-app-a", text)
        report = self.read("report.md")
        self.assertIn("## Fix before importing", report)
        self.assertIn("which is a network in Infoblox", report)
        self.assertIn("| 10.0.1.0/24 | network | default | 10.0.0.0/16 (from this export) |", report)
        self.assertIn("**Checked against Infoblox:** yes", report)

    def test_overlaps_go_to_views_per_account(self):
        with FakeGrid() as grid:
            status, _, err = run("plan", FIXTURE, "-o", self.path("import.csv"), "--wapi", grid.url,
                                 "--allow-http", "--network-view", "aws-{account}", env=CREDENTIALS)
        self.assertEqual(status, 1)
        self.assertIn("must fix: network view 'aws-123456789012' doesn't exist", err)

    def test_rows_the_plan_leaves_out_are_counted(self):
        path = self.path("pools.csv")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("Resource ID,Resource type,Resource CIDR,Resource owner\n"
                         "ipam-pool-1,Pool,10.0.0.0/8,-\nvpc-1,VPC,10.1.0.0/16,111122223333\n")
        status, _, err = run("plan", path, "-o", self.path("x.csv"), "--report", self.path("r.md"),
                             "--network-view", "aws-{account}")
        self.assertEqual(status, 0, err)
        self.assertIn("1 row(s) left out", err)
        self.assertIn("| 1 | 1 | 0 | 0 | 0 | 0 | 1 |", self.read("r.md"))

    def test_split_and_netmask_style(self):
        status, _, err = run("plan", FIXTURE, "-o", self.path("import.csv"), "--split",
                             "--netmask-style", "dotted")
        self.assertEqual(status, 0, err)
        self.assertIn("networkcontainer,10.0.0.0,255.0.0.0,", self.read("import-containers.csv"))
        self.assertNotIn("header-network,", self.read("import-containers.csv"))
        self.assertTrue(self.read("import-networks.csv").startswith("header-network,"))

    def test_bad_input(self):
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                main(["plan", FIXTURE, "-o", self.path("x.csv"), "--ea", "Owner=colour"])
        status, _, err = run("plan", FIXTURE, "-o", self.path("x.csv"), "--comment", "{colour}")
        self.assertEqual(status, 2)
        self.assertIn("unknown field {colour}", err)
        status, _, err = run("plan", self.path("missing.csv"), "-o", self.path("x.csv"))
        self.assertEqual(status, 2)

    def test_credentials(self):
        status, _, err = run("plan", FIXTURE, "-o", self.path("x.csv"), "--wapi", "gm.example.net",
                             env={"INFOBLOX_USERNAME": "", "INFOBLOX_PASSWORD": ""})
        self.assertEqual(status, 2)
        self.assertIn("--wapi needs a user", err)
        with mock.patch.dict(os.environ, {"INFOBLOX_USERNAME": USER}), \
                mock.patch.object(sys.stdin, "isatty", return_value=False):
            os.environ.pop("INFOBLOX_PASSWORD", None)
            status, _, err = run("plan", FIXTURE, "-o", self.path("x.csv"), "--wapi", "gm.example.net")
        self.assertEqual(status, 2)
        self.assertIn("set $INFOBLOX_PASSWORD", err)


if __name__ == "__main__":
    unittest.main()
