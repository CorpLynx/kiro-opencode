import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from topomap import anonymize as anon, cli  # noqa: E402
from topomap.hygiene import untracked_hint  # noqa: E402
from topomap.model import Topology  # noqa: E402

ACCOUNT = "111122223333"
OTHER = "444455556666"
ORG, ROOT, OU = "o-example0001", "r-example1", "ou-0001-example1"
VPC, SUBNET = "vpc-0123456789abcdef0", "subnet-0123abcd"


def sample():
    t = Topology({"collector": "aws", "accounts": [ACCOUNT, OTHER]})
    t.add_node(f"aws:organization:{ORG}", "organization", f"Organization {ORG}", org_id=ORG,
               arn=f"arn:aws:organizations::{ACCOUNT}:organization/{ORG}")
    t.add_node(f"aws:org-root:{ROOT}", "org-root", "Root", f"aws:organization:{ORG}", root_id=ROOT)
    t.add_node(f"aws:ou:{OU}", "ou", "Acme Payments", f"aws:org-root:{ROOT}", ou_id=OU,
               path="Root/Acme Payments", policies={"scp": ["acme-guardrails"]})
    t.add_node(f"aws:account:{ACCOUNT}", "account", f"Acme Prod ({ACCOUNT})", f"aws:ou:{OU}",
               account_id=ACCOUNT, name="Acme Prod", email="ops@acme.example")
    t.add_node(f"aws:account:{OTHER}", "account", f"Acme Dev ({OTHER})", f"aws:ou:{OU}",
               account_id=OTHER, name="Acme Dev")
    t.add_node(f"aws:vpc:{VPC}", "vpc", f"acme-vpc ({VPC})", f"aws:account:{ACCOUNT}",
               vpc_id=VPC, cidrs=["10.20.0.0/16"], tags={"Name": "acme-vpc", "Owner": "dana"})
    t.add_node(f"aws:subnet:{SUBNET}", "subnet", f"acme-subnet ({SUBNET})", f"aws:vpc:{VPC}",
               subnet_id=SUBNET, cidr="10.20.1.0/24")
    t.add_node("aws:customer-gateway:cgw-0123456789abcdef0", "customer-gateway",
               "cgw-0123456789abcdef0", ip="203.0.113.10", device_name="acme-edge1")
    t.add_node("net:device:acme-edge1", "device", "acme-edge1", mgmt_ip="203.0.113.10",
               serial="FOC1234ABCD", sources=["configs/acme-edge1.conf"],
               interfaces=[{"name": "Gi0/0", "addresses": ["198.51.100.7/30"],
                            "description": "uplink to Acme carrier"}])
    t.add_link("vpn", "aws:customer-gateway:cgw-0123456789abcdef0", f"aws:vpc:{VPC}",
               label="vpn-0123456789abcdef0")
    return t


def dumped(topo):
    return topo.dumps()


class AnonymizeTest(unittest.TestCase):
    def setUp(self):
        self.out = anon.anonymize(sample(), ips="public")
        self.text = dumped(self.out)

    def test_nothing_identifying_survives(self):
        for original in (ACCOUNT, OTHER, ORG, ROOT, OU, VPC, SUBNET, "acme", "Acme", "dana",
                         "ops@", "FOC1234ABCD", "carrier", "0123456789abcdef0"):
            self.assertNotIn(original, self.text, original)
        self.assertEqual(self.out.validate(), [])
        self.assertNotIn("warnings", self.out.meta)
        self.assertIs(self.out.meta["anonymized"], True)

    def test_ids_keep_shape(self):
        vpc = self.out.by_kind("vpc")[0]
        self.assertRegex(vpc.id, r"^aws:vpc:vpc-[0-9a-f]{17}$")
        self.assertRegex(self.out.by_kind("subnet")[0].id, r"^aws:subnet:subnet-[0-9a-f]{8}$")
        self.assertRegex(self.out.by_kind("ou")[0].id, r"^aws:ou:ou-[0-9a-f]{4}-[0-9a-f]{8}$")
        for account in self.out.by_kind("account"):
            self.assertRegex(account.attrs["account_id"], r"^\d{12}$")
            self.assertIn(account.attrs["account_id"], account.label)
            self.assertIn(account.attrs["account_id"], account.id)

    def test_structure_is_kept(self):
        parents = {n.kind: self.out.nodes[n.parent].kind for n in self.out.nodes.values()
                   if n.kind in ("subnet", "vpc", "ou") and n.parent}
        self.assertEqual(parents, {"subnet": "vpc", "vpc": "account", "ou": "org-root"})
        self.assertEqual(len(self.out.links), 1)

    def test_names_tags_and_free_text(self):
        ou = self.out.by_kind("ou")[0]
        self.assertRegex(ou.label, r"^ou-\d+$")
        self.assertEqual(ou.attrs["path"], f"Root/{ou.label}")
        self.assertRegex(ou.attrs["policies"]["scp"][0], r"^policy-\d+$")
        vpc = self.out.by_kind("vpc")[0]
        self.assertEqual(sorted(vpc.attrs["tags"]), ["Name", "Owner"])
        self.assertTrue(all(v.startswith(("tag-", "resource-", "vpc-")) for v in vpc.attrs["tags"].values()))
        device = self.out.by_kind("device")[0]
        self.assertEqual(device.attrs["serial"], "serial-1")
        self.assertEqual(device.attrs["sources"], ["source-1"])
        self.assertEqual(device.attrs["interfaces"][0]["description"], "description-1")
        self.assertEqual(device.attrs["interfaces"][0]["name"], "Gi0/0")

    def test_same_value_same_fake(self):
        device = self.out.by_kind("device")[0]
        cgw = self.out.by_kind("customer-gateway")[0]
        self.assertEqual(cgw.attrs["device_name"], device.label)
        self.assertEqual(cgw.attrs["ip"], device.attrs["mgmt_ip"])

    def test_deterministic_and_input_untouched(self):
        original = sample()
        before = dumped(original)
        again = anon.anonymize(original, ips="public")
        self.assertEqual(dumped(again), self.text)
        self.assertEqual(dumped(original), before)

    def test_public_addresses_move_to_documentation_ranges(self):
        t = Topology()
        t.add_node("net:device:r1", "device", "r1", mgmt_ip="8.8.8.8",
                   interfaces=[{"name": "e0", "addresses": ["10.1.2.3/24", "1.1.1.1/30"]}])
        out = anon.anonymize(t, ips="public").by_kind("device")[0]
        mgmt = out.attrs["mgmt_ip"]
        public = out.attrs["interfaces"][0]["addresses"][1]
        self.assertTrue(mgmt.startswith("192.0.2."))
        self.assertTrue(public.startswith("192.0.2.") and public.endswith("/30"))
        self.assertNotEqual(mgmt, public.split("/")[0])
        self.assertEqual(out.attrs["interfaces"][0]["addresses"][0], "10.1.2.3/24")

    def test_keep_leaves_addresses_and_keep_names_leaves_names(self):
        t = Topology()
        t.add_node("net:device:coreswitch", "device", "coreswitch", mgmt_ip="8.8.8.8")
        out = anon.anonymize(t, ips="keep", keep_names=True).by_kind("device")[0]
        self.assertEqual((out.label, out.attrs["mgmt_ip"]), ("coreswitch", "8.8.8.8"))

    def test_bad_mode(self):
        with self.assertRaises(ValueError):
            anon.anonymize(sample(), ips="nope")

    @unittest.skipUnless(shutil.which("ipredact") or anon.importlib.util.find_spec("ipredact"),
                         "needs the ipredact tool")
    def test_ipredact_mode_changes_addresses_and_keeps_containment(self):
        out = anon.anonymize(sample(), ips="ipredact")
        text = dumped(out)
        self.assertNotIn("203.0.113.10", text)
        self.assertNotIn("10.20.", text)
        vpc, subnet = out.by_kind("vpc")[0], out.by_kind("subnet")[0]
        net = vpc.attrs["cidrs"][0].split(".")[:2]
        self.assertEqual(subnet.attrs["cidr"].split(".")[:2], net)


class CliTest(unittest.TestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = cli.main(list(argv))
        return status, out.getvalue(), err.getvalue()

    def test_command(self):
        with tempfile.TemporaryDirectory() as d:
            src, dst = os.path.join(d, "in.json"), os.path.join(d, "out.json")
            sample().save(src)
            status, _, err = self.run_cli("anonymize", src, "--ips", "public", "-o", dst)
            self.assertEqual(status, 0, err)
            with open(dst) as f:
                text = f.read()
            self.assertNotIn(ACCOUNT, text)
            self.assertEqual(json.loads(text)["meta"]["anonymized"], True)

    def test_output_folders_are_created(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "in.json")
            sample().save(src)
            out = os.path.join(d, "topomap-out", "nested", "a.json")
            self.assertEqual(self.run_cli("filter", src, "-o", out)[0], 0)
            svg = os.path.join(d, "topomap-out", "a.svg")
            self.assertEqual(self.run_cli("render", out, "-o", svg)[0], 0)
            self.assertTrue(os.path.exists(svg))

    def test_missing_ipredact_is_a_clear_error(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "in.json")
            sample().save(src)
            with mock.patch("shutil.which", return_value=None), \
                    mock.patch("importlib.util.find_spec", return_value=None):
                status, _, err = self.run_cli("anonymize", src, "-o", os.path.join(d, "o.json"))
            self.assertEqual(status, 2)
            self.assertIn("pip install ./tools/ip-redactor", err)

    def test_key_file_needs_ipredact_mode(self):
        status, _, err = self.run_cli("anonymize", "-", "--ips", "keep", "--ip-key-file", "k")
        self.assertEqual(status, 2)
        self.assertIn("--ip-key-file", err)


@unittest.skipUnless(shutil.which("git"), "needs git")
class HygieneTest(unittest.TestCase):
    def test_hint_only_for_unignored_files_in_a_repository(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(untracked_hint(os.path.join(d, "a.json")))  # not a repository
            subprocess.run(["git", "init", "-q", d], check=True)
            with open(os.path.join(d, ".gitignore"), "w") as f:
                f.write("topomap-out/\n")
            self.assertIn("isn't ignored", untracked_hint(os.path.join(d, "a.json")))
            os.makedirs(os.path.join(d, "topomap-out"))
            self.assertIsNone(untracked_hint(os.path.join(d, "topomap-out", "a.json")))
            self.assertIsNone(untracked_hint("-"))

    def test_collect_style_output_prints_a_note_but_anonymize_does_not(self):
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["git", "init", "-q", d], check=True)
            src = os.path.join(d, "in.json")
            sample().save(src)
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                cli.main(["filter", src, "-o", os.path.join(d, "f.json")])
                noted = err.getvalue()
                err.truncate(0)
                err.seek(0)
                cli.main(["anonymize", src, "--ips", "public", "-o", os.path.join(d, "a.json")])
            self.assertIn("note:", noted)
            self.assertNotIn("note:", err.getvalue())


if __name__ == "__main__":
    unittest.main()
