import io
import json
import os
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from topomapper.cli import main  # noqa: E402
from topomapper.model import Topology  # noqa: E402
from topomapper.render import render  # noqa: E402

try:
    import boto3
    from botocore.stub import Stubber
    from moto import mock_aws
    from topomapper.collectors import aws_org
    from topomapper.collectors.aws import ReadOnlyClient, ReadOnlyError
except ImportError:  # pragma: no cover
    boto3 = None

FAKE_AWS = {
    "AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing",
    "AWS_SESSION_TOKEN": "testing", "AWS_DEFAULT_REGION": "us-east-1",
    "AWS_CONFIG_FILE": os.devnull, "AWS_SHARED_CREDENTIALS_FILE": os.devnull,
}
DENY_REGIONS = json.dumps({"Version": "2012-10-17", "Statement": [{
    "Effect": "Deny", "Action": "*", "Resource": "*",
    "Condition": {"StringNotEquals": {"aws:RequestedRegion": ["us-east-1"]}}}]})
TAG_POLICY = json.dumps({"tags": {"env": {"tag_key": {"@@assign": "env"}}}})


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        status = main(list(argv))
    return status, out.getvalue(), err.getvalue()


def build_org():
    """Root > Security, Workloads > (Prod, Dev); accounts in each, an SCP on
    Workloads, a tag policy on Prod, and a delegated admin in Security."""
    org = boto3.client("organizations", region_name="us-east-1")
    org.create_organization(FeatureSet="ALL")
    root = org.list_roots()["Roots"][0]["Id"]
    for policy_type in ("SERVICE_CONTROL_POLICY", "TAG_POLICY"):
        org.enable_policy_type(RootId=root, PolicyType=policy_type)

    def ou(parent, name):
        return org.create_organizational_unit(ParentId=parent, Name=name)["OrganizationalUnit"]["Id"]

    def account(name, parent):
        aid = org.create_account(AccountName=name, Email=f"{name}@example.net")[
            "CreateAccountStatus"]["AccountId"]
        org.move_account(AccountId=aid, SourceParentId=root, DestinationParentId=parent)
        return aid

    ids = {"root": root, "security": ou(root, "Security"), "workloads": ou(root, "Workloads")}
    ids["prod"] = ou(ids["workloads"], "Prod")
    ids["dev"] = ou(ids["workloads"], "Dev")
    ids["audit"] = account("audit", ids["security"])
    ids["app-prod"] = account("app-prod", ids["prod"])
    ids["app-dev"] = account("app-dev", ids["dev"])

    scp = org.create_policy(Content=DENY_REGIONS, Description="only us-east-1", Name="DenyRegions",
                            Type="SERVICE_CONTROL_POLICY")["Policy"]["PolicySummary"]["Id"]
    org.attach_policy(PolicyId=scp, TargetId=ids["workloads"])
    tags = org.create_policy(Content=TAG_POLICY, Description="env tag", Name="EnvTag",
                             Type="TAG_POLICY")["Policy"]["PolicySummary"]["Id"]
    org.attach_policy(PolicyId=tags, TargetId=ids["prod"])
    org.register_delegated_administrator(AccountId=ids["audit"],
                                         ServicePrincipal="guardduty.amazonaws.com")
    org.tag_resource(ResourceId=ids["app-prod"], Tags=[{"Key": "env", "Value": "prod"}])
    return org, ids


@unittest.skipIf(boto3 is None, "needs boto3 and moto")
class OrgTest(unittest.TestCase):
    def setUp(self):
        env = mock.patch.dict(os.environ, FAKE_AWS)
        env.start()
        self.addCleanup(env.stop)
        self.moto = mock_aws()
        self.moto.start()
        self.addCleanup(self.moto.stop)
        self.client, self.ids = build_org()

    def collect(self, **kwargs):
        return aws_org.collect(ReadOnlyClient(self.client), **kwargs)

    def test_structure(self):
        topo = self.collect()
        self.assertEqual(topo.validate(), [])
        ids = self.ids
        org_node = topo.by_kind("organization")[0]
        root = f"aws:org-root:{ids['root']}"
        self.assertEqual(topo.nodes[root].parent, org_node.id)
        self.assertEqual(topo.nodes[root].attrs["policy_types"], ["scp", "tag"])
        self.assertEqual(topo.nodes[f"aws:ou:{ids['prod']}"].parent, f"aws:ou:{ids['workloads']}")
        self.assertEqual(topo.nodes[f"aws:ou:{ids['prod']}"].attrs["path"], "Root/Workloads/Prod")

        app = topo.nodes[f"aws:account:{ids['app-prod']}"]
        self.assertEqual(app.parent, f"aws:ou:{ids['prod']}")
        self.assertEqual(app.label, f"app-prod ({ids['app-prod']})")
        self.assertEqual(app.attrs["status"], "ACTIVE")
        self.assertNotIn("email", app.attrs)  # personal data only on request

        management = org_node.attrs["management_account_id"]
        self.assertIs(topo.nodes[f"aws:account:{management}"].attrs["management"], True)
        self.assertEqual(topo.nodes[f"aws:account:{management}"].parent, root)
        self.assertEqual(topo.meta["collector"], "aws-org")
        self.assertEqual(topo.meta["ous"], sorted(
            ids[k] for k in ("security", "workloads", "prod", "dev")))
        self.assertEqual(len(topo.meta["accounts"]), 4)

    def test_empty_ous_are_flagged(self):
        self.client.create_organizational_unit(ParentId=self.ids["root"], Name="Unused")
        topo = self.collect()
        for ou in topo.by_kind("ou"):
            self.assertEqual(bool(ou.attrs.get("empty")), not topo.children(ou.id), ou.label)
        self.assertTrue(any(ou.attrs.get("empty") and ou.label == "Unused"
                            for ou in topo.by_kind("ou")))

    def test_policies_and_delegated_admins(self):
        topo = self.collect()
        ids = self.ids
        self.assertEqual(topo.nodes[f"aws:ou:{ids['workloads']}"].attrs["policies"],
                         {"scp": ["DenyRegions"]})
        self.assertEqual(topo.nodes[f"aws:ou:{ids['prod']}"].attrs["policies"], {"tag": ["EnvTag"]})
        # FullAWSAccess is attached everywhere; it's hidden unless asked for.
        self.assertNotIn("policies", topo.nodes[f"aws:org-root:{ids['root']}"].attrs)
        self.assertEqual(topo.nodes[f"aws:account:{ids['audit']}"].attrs["delegated_admin_for"],
                         ["guardduty.amazonaws.com"])

        everything = self.collect(include=["aws-managed-policies"])
        self.assertEqual(everything.nodes[f"aws:org-root:{ids['root']}"].attrs["policies"]["scp"],
                         ["FullAWSAccess"])

        none = self.collect(policies="none")
        self.assertFalse(any("policies" in n.attrs for n in none.nodes.values()))

    def test_policy_nodes(self):
        topo = self.collect(policies="nodes")
        self.assertEqual(topo.validate(), [])
        policy = next(n for n in topo.by_kind("org-policy") if n.label == "DenyRegions")
        self.assertEqual(policy.attrs["type"], "scp")
        links = [l for l in topo.links.values() if l.source == policy.id]
        self.assertEqual([l.target for l in links], [f"aws:ou:{self.ids['workloads']}"])
        self.assertTrue(links[0].directed)

    def test_extras(self):
        topo = self.collect(include=["tags", "emails"])
        app = topo.nodes[f"aws:account:{self.ids['app-prod']}"]
        self.assertEqual(app.attrs["tags"], {"env": "prod"})
        self.assertEqual(app.attrs["email"], "app-prod@example.net")

    def test_read_only(self):
        with self.assertRaises(ReadOnlyError):
            ReadOnlyClient(self.client).create_organizational_unit(ParentId="r-x", Name="nope")

    def test_merge_with_network_snapshot(self):
        """The same account from both collectors is one node: inside its OU,
        holding its region and VPCs."""
        org = self.collect()
        net = Topology({"collector": "aws"})
        aid = self.ids["app-prod"]
        net.add_node(f"aws:account:{aid}", "account", aid, account_id=aid)
        net.add_node(f"aws:region:{aid}:us-east-1", "region", "us-east-1", f"aws:account:{aid}")
        net.add_node("aws:vpc:vpc-0a1", "vpc", "prod (vpc-0a1)", f"aws:region:{aid}:us-east-1",
                     cidrs=["10.60.0.0/16"])
        with tempfile.TemporaryDirectory() as d:
            org.save(os.path.join(d, "org.json"))
            net.save(os.path.join(d, "net.json"))
            for order in (("org.json", "net.json"), ("net.json", "org.json")):
                status, out, err = run("merge", *(os.path.join(d, f) for f in order))
                self.assertEqual(status, 0, err)
                merged = Topology.loads(out)
                account = merged.nodes[f"aws:account:{aid}"]
                self.assertEqual(account.parent, f"aws:ou:{self.ids['prod']}")
                self.assertEqual(account.label, f"app-prod ({aid})")
                self.assertEqual(merged.ancestors("aws:vpc:vpc-0a1")[:2],
                                 [f"aws:region:{aid}:us-east-1", f"aws:account:{aid}"])
                self.assertEqual(merged.validate(), [])

    def test_render(self):
        topo = self.collect(policies="nodes")
        drawio = ET.fromstring(render(topo, "drawio"))
        styles = {el.get("id"): (el.find("mxCell") if el.tag == "UserObject" else el).get("style", "")
                  for el in drawio.iter() if el.get("id")}
        management = topo.by_kind("organization")[0].attrs["management_account_id"]
        self.assertIn("organizations_management_account", styles[f"aws:account:{management}"])
        self.assertIn("organizations_account;", styles[f"aws:account:{self.ids['app-prod']}"])
        self.assertIn("container=1", styles[f"aws:ou:{self.ids['prod']}"])
        self.assertIn("group_aws_cloud_alt", styles[topo.by_kind("organization")[0].id])
        svg = render(topo, "svg")
        ET.fromstring(svg)
        self.assertIn("SCP: DenyRegions", svg)
        self.assertIn("subgraph", render(topo, "mermaid"))

    def test_cli(self):
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "org.json")
            with mock.patch.object(aws_org, "_new_client", lambda *a, **k: self.client):
                status, _, err = run("collect", "aws-org", "--policies", "nodes",
                                     "--include", "tags", "-o", out)
            self.assertEqual(status, 0, err)
            topo = Topology.load(out)
            self.assertEqual(len(topo.meta["accounts"]), 4)
            err = io.StringIO()
            with redirect_stderr(err), self.assertRaises(SystemExit) as exit_:
                main(["collect", "aws-org", "--include", "passwords"])
            self.assertEqual(exit_.exception.code, 2)
            self.assertIn("unknown --include value", err.getvalue())


@unittest.skipIf(boto3 is None, "needs boto3 and moto")
class ErrorTest(unittest.TestCase):
    def stubbed(self, operation, code):
        client = boto3.client("organizations", region_name="us-east-1",
                              aws_access_key_id="x", aws_secret_access_key="x")
        stub = Stubber(client)
        if operation == "list_roots":
            stub.add_response("describe_organization", {"Organization": {
                "Id": "o-exampleorgid", "MasterAccountId": "111122223333", "FeatureSet": "ALL"}})
        stub.add_client_error(operation, service_error_code=code)
        stub.activate()
        return ReadOnlyClient(client)

    def test_not_in_an_organization(self):
        with self.assertRaisesRegex(RuntimeError, "isn't part of an AWS Organization"):
            aws_org.collect(self.stubbed("describe_organization", "AWSOrganizationsNotInUseException"))

    def test_member_account_is_told_who_to_run_as(self):
        with self.assertRaisesRegex(RuntimeError, "management account"):
            aws_org.collect(self.stubbed("list_roots", "AccessDeniedException"))


if __name__ == "__main__":
    unittest.main()
