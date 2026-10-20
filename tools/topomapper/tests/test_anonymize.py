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
from topomapper import anonymize as anon, cli  # noqa: E402
from topomapper.hygiene import untracked_hint  # noqa: E402
from topomapper.model import Topology, link_id  # noqa: E402

ACCOUNT = "111122223333"
OTHER = "444455556666"
ORG, ROOT, OU = "o-example0001", "r-0001", "ou-0001-example1"
VPC, SUBNET = "vpc-0123456789abcdef0", "subnet-0123abcd"
INSTANCE = "i-0123456789abcdef1"


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
    t.add_node(f"aws:instance:{INSTANCE}", "instance", f"acme-edge1 ({INSTANCE})", f"aws:subnet:{SUBNET}",
               instance_id=INSTANCE, private_ips=["10.20.1.7"], public_ip="203.0.113.10",
               description="uplink to Acme carrier", sources=["exports/acme-edge1.json"])
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
                         "ops@", "edge1", "carrier", "0123456789abcdef"):
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
        instance = self.out.by_kind("instance")[0]
        self.assertEqual(instance.attrs["description"], "description-1")
        self.assertEqual(instance.attrs["sources"], ["source-1"])

    def test_same_value_same_fake(self):
        instance = self.out.by_kind("instance")[0]
        cgw = self.out.by_kind("customer-gateway")[0]
        self.assertEqual(cgw.attrs["device_name"], instance.label.split(" (")[0])
        self.assertEqual(cgw.attrs["ip"], instance.attrs["public_ip"])

    def test_deterministic_and_input_untouched(self):
        original = sample()
        before = dumped(original)
        again = anon.anonymize(original, ips="public")
        self.assertEqual(dumped(again), self.text)
        self.assertEqual(dumped(original), before)

    def test_public_addresses_move_to_documentation_ranges(self):
        t = Topology()
        t.add_node("aws:vpc:vpc-1", "vpc", "vpc-1", cidrs=["10.1.0.0/16", "1.1.1.0/30"])
        t.add_node("aws:instance:i-1", "instance", "i-1", "aws:vpc:vpc-1", public_ip="8.8.8.8",
                   private_ips=["10.1.2.3"])
        out = anon.anonymize(t, ips="public")
        instance, vpc = out.by_kind("instance")[0], out.by_kind("vpc")[0]
        public, cidr = instance.attrs["public_ip"], vpc.attrs["cidrs"][1]
        self.assertTrue(public.startswith("192.0.2."))
        self.assertTrue(cidr.startswith("192.0.2.") and cidr.endswith("/30"))
        self.assertNotEqual(public, cidr.split("/")[0])
        self.assertEqual(instance.attrs["private_ips"], ["10.1.2.3"])
        self.assertEqual(vpc.attrs["cidrs"][0], "10.1.0.0/16")

    def test_keep_leaves_addresses_and_keep_names_leaves_names(self):
        t = Topology()
        t.add_node("aws:iam-role:123456789012:coreapp", "iam-role", "coreapp", role_name="coreapp")
        t.add_node("aws:instance:i-1", "instance", "i-1", public_ip="8.8.8.8")
        out = anon.anonymize(t, ips="keep", keep_names=True)
        self.assertEqual(out.by_kind("iam-role")[0].label, "coreapp")
        self.assertEqual(out.by_kind("instance")[0].attrs["public_ip"], "8.8.8.8")

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


OIDC = "ABCDEF0123456789ABCDEF0123456789"


def ai_sample():
    t = Topology({"collector": "aws"})
    t.add_node(f"aws:account:{ACCOUNT}", "account", f"Acme Prod ({ACCOUNT})", account_id=ACCOUNT,
               name="Acme Prod")
    t.add_node(f"aws:region:{ACCOUNT}:us-east-1", "region", "us-east-1", f"aws:account:{ACCOUNT}")
    t.add_node(f"aws:bedrock:{ACCOUNT}:us-east-1", "bedrock", "Amazon Bedrock",
               f"aws:region:{ACCOUNT}:us-east-1", models_shown=2)
    t.add_node(f"aws:bedrock-model:{ACCOUNT}:us-east-1:amazon.titan-text-express-v1", "bedrock-model",
               "Titan Text G1 - Express", f"aws:bedrock:{ACCOUNT}:us-east-1", provider="Amazon",
               source="foundation", model_id="amazon.titan-text-express-v1")
    t.add_node(f"aws:bedrock-model:{ACCOUNT}:us-east-1:custom/acme-claims-tuned", "bedrock-model",
               "acme-claims-tuned", f"aws:bedrock:{ACCOUNT}:us-east-1", source="custom",
               model_name="acme-claims-tuned", base_model="Titan Text G1 - Express",
               arn=f"arn:aws:bedrock:us-east-1:{ACCOUNT}:custom-model/acme-claims-tuned")
    t.add_node(f"aws:iam:{ACCOUNT}", "iam", "IAM", f"aws:account:{ACCOUNT}", summary="1 role, 1 user")
    t.add_node(f"aws:iam-role:{ACCOUNT}:acme-deploy", "iam-role", "acme-deploy",
               f"aws:iam:{ACCOUNT}", role_name="acme-deploy",
               arn=f"arn:aws:iam::{ACCOUNT}:role/acme-deploy", trusted_accounts=[OTHER],
               trusted_principals=[{"account": OTHER, "type": "role", "name": "acme-builder"}],
               trusted_federated=[f"arn:aws:iam::{ACCOUNT}:oidc-provider/oidc.eks.us-east-1."
                                  f"amazonaws.com/id/{OIDC}"],
               attached_policies=["acme-s3-readonly"], admin_access=True)
    t.add_node(f"aws:iam-user:{ACCOUNT}:dana", "iam-user", "dana", f"aws:iam:{ACCOUNT}",
               user_name="dana", mfa_enabled=False, active_access_keys=1)
    t.add_node(f"aws:eks-cluster:{ACCOUNT}:us-east-1:acme-platform", "eks-cluster", "acme-platform",
               f"aws:region:{ACCOUNT}:us-east-1", cluster_name="acme-platform", version="1.30",
               oidc_issuer=f"https://oidc.eks.us-east-1.amazonaws.com/id/{OIDC}",
               nodegroups=[{"name": "acme-workers", "desired": 2}])
    t.add_link("iam-trust", f"aws:account:{ACCOUNT}", f"aws:iam-role:{ACCOUNT}:acme-deploy",
               directed=True, principal="root")
    return t


class NewKindsTest(unittest.TestCase):
    def test_names_ids_and_issuers_are_replaced(self):
        out = anon.anonymize(ai_sample(), ips="keep")
        text = dumped(out)
        for original in (ACCOUNT, "acme", "Acme", OIDC, "dana", "claims-tuned"):
            self.assertNotIn(original, text, original)
        self.assertNotIn("warnings", out.meta)
        self.assertEqual(out.validate(), [])
        self.assertIn("Titan Text G1 - Express", text)         # public model names stay
        self.assertIn("amazon.titan-text-express-v1", text)
        self.assertRegex(text, r"/id/[0-9A-F]{32}")
        self.assertEqual(len(out.links), 1)

    def test_strict_keeps_only_the_safe_facts(self):
        out = anon.anonymize(ai_sample(), ips="keep", strict=True)
        role = out.by_kind("iam-role")[0]
        self.assertEqual(sorted(role.attrs), ["admin_access", "arn"])
        user = out.by_kind("iam-user")[0]
        self.assertEqual(sorted(user.attrs), ["active_access_keys", "mfa_enabled"])
        cluster = out.by_kind("eks-cluster")[0]
        self.assertEqual(sorted(cluster.attrs), ["nodegroups", "version"])
        self.assertEqual([sorted(g) for g in cluster.attrs["nodegroups"]], [["desired", "name"]])
        custom = [n for n in out.by_kind("bedrock-model") if n.attrs.get("source") == "custom"][0]
        self.assertNotIn("acme", dumped(out))
        self.assertEqual(custom.attrs["source"], "custom")


class BedrockGrantsTest(unittest.TestCase):
    def sample(self):
        t = Topology({"collector": "aws"})
        t.add_node(f"aws:account:{ACCOUNT}", "account", account_id=ACCOUNT)
        t.add_node(f"aws:bedrock:{ACCOUNT}:us-east-1", "bedrock", "Amazon Bedrock",
                   f"aws:account:{ACCOUNT}", models_shown=1, granted_all_to=["acme-admin"])
        t.add_node(f"aws:bedrock-model:{ACCOUNT}:us-east-1:amazon.nova-pro-v1:0", "bedrock-model",
                   "Nova Pro", f"aws:bedrock:{ACCOUNT}:us-east-1", provider="Amazon",
                   evidence=["granted"], granted_to=["acme-n8n-instance"], invocable=True)
        return t

    def test_principal_names_are_replaced_even_without_iam_nodes(self):
        out = anon.anonymize(self.sample(), ips="keep")
        text = dumped(out)
        self.assertNotIn("acme", text)
        self.assertIn("Nova Pro", text)
        self.assertNotIn("warnings", out.meta)

    def test_strict_keeps_the_evidence_but_not_the_names(self):
        out = anon.anonymize(self.sample(), ips="keep", strict=True)
        model = out.by_kind("bedrock-model")[0]
        self.assertEqual(sorted(model.attrs), ["evidence", "invocable", "provider"])
        self.assertNotIn("granted_to", model.attrs)
        self.assertNotIn("granted_all_to", out.by_kind("bedrock")[0].attrs)


class HardeningTest(unittest.TestCase):
    def test_link_order_survives_renaming(self):
        t = Topology()
        names = [f"app{chr(97 + i)}" for i in range(11)]
        for name in names:
            t.add_node(f"aws:iam-role:{ACCOUNT}:{name}", "iam-role", name, role_name=name)
        t.add_link("vpc-peering", f"aws:iam-role:{ACCOUNT}:appb", f"aws:iam-role:{ACCOUNT}:appk",
                   source_port="e0", target_port="e1")
        out = anon.anonymize(t, ips="keep")
        (link,) = out.links.values()
        self.assertLess(link.source, link.target)
        self.assertEqual(link.id, link_id("vpc-peering", link.source, link.target,
                                          link.attrs["source_port"], link.attrs["target_port"]))
        self.assertEqual((link.attrs["source_port"], link.attrs["target_port"]), ("e1", "e0"))
        self.assertEqual(out.validate(), [])

    def test_fakes_never_equal_a_real_value(self):
        real_one, real_two = "vpc-" + "0" * 16 + "1", "vpc-" + "a" * 17
        t = Topology()
        t.add_node(f"aws:vpc:{real_one}", "vpc", "x1")
        t.add_node(f"aws:vpc:{real_two}", "vpc", "x2")
        t.add_node(f"aws:iam-role:{ACCOUNT}:role-1", "iam-role", "role-1")
        t.add_node(f"aws:iam-role:{ACCOUNT}:coreapp", "iam-role", "coreapp")
        out = anon.anonymize(t, ips="keep")
        ids = {n.id.split(":", 2)[2] for n in out.by_kind("vpc")}
        self.assertEqual(len(ids), 2)
        self.assertFalse(ids & {real_one, real_two})
        self.assertNotIn("warnings", out.meta)
        labels = sorted(n.label for n in out.by_kind("iam-role"))
        self.assertEqual(len(set(labels)), 2)
        self.assertNotIn("coreapp", labels)

    def test_strict_drops_unfamiliar_and_free_text_attributes(self):
        t = sample()
        t.nodes["aws:vpc:" + VPC].attrs["owner_notes"] = "call dana"
        out = anon.anonymize(t, ips="public", strict=True)
        vpc = out.by_kind("vpc")[0]
        self.assertEqual(sorted(vpc.attrs), ["cidrs", "vpc_id"])
        instance = out.by_kind("instance")[0]
        self.assertEqual(sorted(instance.attrs), ["instance_id", "private_ips", "public_ip"])
        self.assertIs(out.meta["strict"], True)
        self.assertNotIn("dana", dumped(out))
        self.assertIn("owner_notes", anon.anonymize(t, ips="public").by_kind("vpc")[0].attrs)

    def test_leak_check_reports_names_and_addresses(self):
        mapper = anon._Mapper("public")
        mapper.names = {"zebra": "device-1", "vpc": "resource-1"}
        mapper.ipv4 = {"8.8.8.8": "192.0.2.1"}
        mapper.originals = {"o-example0002"}
        text = '{"kind": "vpc", "label": "Zebra core", "ip": "8.8.8.8", "x": "device-1"}'
        self.assertEqual(anon._survivors(mapper, text),
                         {"public address": 1, "name": 1})
        self.assertEqual(anon._survivors(mapper, '{"label": "device-1", "ip": "18.8.8.80"}'), {})
        self.assertEqual(anon._survivors(mapper, "o-example0002"), {"identifier": 1})

    def test_ipredact_timeout_is_an_error(self):
        with mock.patch.object(anon, "_ipredact_command", return_value=["ipredact"]), \
                mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ipredact", 1)):
            with self.assertRaisesRegex(RuntimeError, "giving up"):
                anon._ipredact("{}", None)

    def test_keep_names_leaves_tag_values(self):
        out = anon.anonymize(sample(), ips="keep", keep_names=True)
        self.assertEqual(out.by_kind("vpc")[0].attrs["tags"]["Owner"], "dana")


class PrivateOutputTest(unittest.TestCase):
    def run_cli(self, *argv):
        with contextlib.redirect_stderr(io.StringIO()) as err, \
                contextlib.redirect_stdout(io.StringIO()):
            status = cli.main(list(argv))
        return status, err.getvalue()

    @unittest.skipIf(os.name == "nt", "Unix modes")
    def test_real_output_is_private_and_anonymized_output_is_not(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "in.json")
            sample().save(src)
            real, shared = os.path.join(d, "out", "real.json"), os.path.join(d, "shared.json")
            self.assertEqual(self.run_cli("topology", "filter", src, real)[0], 0)
            self.assertEqual(self.run_cli("topology", "anonymize", src, "--ips", "keep", shared)[0], 0)
            mode = lambda p: os.stat(p).st_mode & 0o777  # noqa: E731
            self.assertEqual(mode(real), 0o600)
            self.assertEqual(mode(os.path.dirname(real)), 0o700)
            self.assertNotEqual(mode(shared), 0o600)
            with open(real, "w") as f:  # an old, looser file is tightened on rewrite
                f.write("x")
            os.chmod(real, 0o644)
            self.run_cli("topology", "filter", src, real)
            self.assertEqual(mode(real), 0o600)

    @unittest.skipUnless(shutil.which("git") and sys.platform in ("darwin", "linux"), "needs git")
    def test_redirected_stdout_gets_the_git_note(self):
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["git", "init", "-q", d], check=True)
            src = os.path.join(d, "in.json")
            sample().save(src)
            root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            with open(os.path.join(d, "out.json"), "w") as out:
                done = subprocess.run([sys.executable, "-m", "topomapper", "topology", "filter", src],
                                      cwd=root, stdout=out, stderr=subprocess.PIPE, text=True)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("out.json", done.stderr)
            self.assertIn("isn't ignored", done.stderr)


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
            status, _, err = self.run_cli("topology", "anonymize", src, "--ips", "public", dst)
            self.assertEqual(status, 0, err)
            with open(dst) as f:
                text = f.read()
            self.assertNotIn(ACCOUNT, text)
            self.assertEqual(json.loads(text)["meta"]["anonymized"], True)

    def test_output_folders_are_created(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "in.json")
            sample().save(src)
            out = os.path.join(d, "topomapper-out", "nested", "a.json")
            self.assertEqual(self.run_cli("topology", "filter", src, out)[0], 0)
            svg = os.path.join(d, "topomapper-out", "a.svg")
            self.assertEqual(self.run_cli("diagram", "draw", out, svg)[0], 0)
            self.assertTrue(os.path.exists(svg))

    def test_missing_ipredact_is_a_clear_error(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "in.json")
            sample().save(src)
            with mock.patch("shutil.which", return_value=None), \
                    mock.patch("importlib.util.find_spec", return_value=None):
                status, _, err = self.run_cli("topology", "anonymize", src, os.path.join(d, "o.json"))
            self.assertEqual(status, 2)
            self.assertIn("pip install ./tools/ip-redactor", err)

    def test_key_file_needs_ipredact_mode(self):
        status, _, err = self.run_cli("topology", "anonymize", "-", "--ips", "keep", "--ip-key-file", "k")
        self.assertEqual(status, 2)
        self.assertIn("--ip-key-file", err)


@unittest.skipUnless(shutil.which("git"), "needs git")
class HygieneTest(unittest.TestCase):
    def test_hint_only_for_unignored_files_in_a_repository(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(untracked_hint(os.path.join(d, "a.json")))  # not a repository
            subprocess.run(["git", "init", "-q", d], check=True)
            with open(os.path.join(d, ".gitignore"), "w") as f:
                f.write("topomapper-out/\n")
            self.assertIn("isn't ignored", untracked_hint(os.path.join(d, "a.json")))
            os.makedirs(os.path.join(d, "topomapper-out"))
            self.assertIsNone(untracked_hint(os.path.join(d, "topomapper-out", "a.json")))
            self.assertIsNone(untracked_hint("-"))

    def test_collect_style_output_prints_a_note_but_anonymize_does_not(self):
        with tempfile.TemporaryDirectory() as d:
            subprocess.run(["git", "init", "-q", d], check=True)
            src = os.path.join(d, "in.json")
            sample().save(src)
            err = io.StringIO()
            with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
                cli.main(["topology", "filter", src, os.path.join(d, "f.json")])
                noted = err.getvalue()
                err.truncate(0)
                err.seek(0)
                cli.main(["topology", "anonymize", src, "--ips", "public", os.path.join(d, "a.json")])
            self.assertIn("note:", noted)
            self.assertNotIn("note:", err.getvalue())


if __name__ == "__main__":
    unittest.main()


class InventoryTest(unittest.TestCase):
    """What `topomapper account collect resources` adds: ARNs in IDs, IAM unique IDs
    and UUIDs as resource IDs."""

    def test_arns_unique_ids_and_uuids_are_replaced(self):
        t = Topology({"collector": "aws"})
        t.add_node(f"aws:account:{ACCOUNT}", "account", ACCOUNT, account_id=ACCOUNT)
        group = f"aws:service:{ACCOUNT}:global:iam"
        t.add_node(group, "aws-service", "IAM", f"aws:account:{ACCOUNT}", service="iam")
        role = f"arn:aws:iam::{ACCOUNT}:role/acme-payments-runtime"
        t.add_node(f"aws:resource:{role}", "aws-resource", "acme-payments-runtime", group, arn=role,
                   resource_id="AROAEXAMPLE1234567890", service="iam",
                   resource_type="AWS::IAM::Role", found_by=["config"])
        key_id = "1f0e2d3c-4b5a-6978-8091-a2b3c4d5e6f7"
        key = f"arn:aws:kms:us-east-1:{ACCOUNT}:key/{key_id}"
        t.add_node(f"aws:resource:{key}", "aws-resource", key_id, group, arn=key,
                   resource_id=key_id, service="kms", resource_type="AWS::KMS::Key",
                   found_by=["config"], relationships=[
                       {"relation": "Is used by", "type": "AWS::IAM::Role",
                        "id": "AROAEXAMPLE1234567890"}])
        out = anon.anonymize(t, ips="public")
        text = dumped(out)
        for original in (ACCOUNT, "acme", "AROAEXAMPLE1234567890", key_id):
            self.assertNotIn(original, text)
        self.assertEqual(out.validate(), [])
        self.assertFalse([w for w in out.meta.get("warnings", []) if "anonymize" in w])
        node = next(n for n in out.by_kind("aws-resource") if n.attrs["service"] == "kms")
        # The same fake everywhere: in the ID, the ARN, the label and the relationship.
        fake_uuid = node.attrs["resource_id"]
        self.assertRegex(fake_uuid, r"^00000000-0000-4000-8000-fa4e[0-9a-f]{8}$")
        self.assertIn(fake_uuid, node.id)
        self.assertEqual(node.label, fake_uuid)
        role_node = next(n for n in out.by_kind("aws-resource") if n.attrs["service"] == "iam")
        self.assertEqual(node.attrs["relationships"][0]["id"], role_node.attrs["resource_id"])
        self.assertTrue(role_node.attrs["resource_id"].startswith("AROA"))
        strict = anon.anonymize(t, ips="public", strict=True)
        kept = next(n for n in strict.by_kind("aws-resource") if n.attrs["service"] == "kms").attrs
        self.assertEqual(sorted(kept), ["arn", "found_by", "relationships", "resource_id",
                                        "resource_type", "service"])
