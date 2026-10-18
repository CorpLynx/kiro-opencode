import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gcp_fake  # noqa: E402
from gcp_fake import AW_FOLDER, CRM, HOST, KEYS, NONPROD, ORG, ORG_POLICY, PROD, SHARED  # noqa: E402
from topomap.cli import main  # noqa: E402
from topomap.collectors import gcp_org  # noqa: E402
from topomap.model import Topology  # noqa: E402
from topomap.render import render  # noqa: E402

DENIED = (403, {"error": {"code": 403, "status": "PERMISSION_DENIED", "message": "denied"}})


def collect(overrides=None, **kwargs):
    api, fake = gcp_fake.api(overrides)
    return gcp_org.collect(api, **kwargs), fake


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        status = main(list(argv))
    return status, out.getvalue(), err.getvalue()


class HierarchyTest(unittest.TestCase):
    def test_folders_and_projects(self):
        topo, _ = collect()
        self.assertEqual(topo.validate(), [])
        org = topo.nodes[f"gcp:organization:{ORG}"]
        self.assertEqual(org.label, f"example.net ({ORG})")
        aw = topo.nodes[f"gcp:folder:{AW_FOLDER}"]
        self.assertEqual(aw.parent, org.id)
        for folder, name in ((PROD, "Prod"), (NONPROD, "NonProd"), (SHARED, "Shared")):
            node = topo.nodes[f"gcp:folder:{folder}"]
            self.assertEqual(node.parent, aw.id)
            self.assertEqual(node.attrs["path"], f"Assured Workloads/{name}")
        host = topo.nodes[f"gcp:project:{HOST}"]
        self.assertEqual(host.parent, f"gcp:folder:{SHARED}")
        self.assertEqual(host.attrs["project_number"], gcp_fake.NUMBERS[HOST])
        self.assertEqual(host.attrs["path"], "Assured Workloads/Shared")
        self.assertNotIn("labels", host.attrs)  # only on request
        self.assertEqual(topo.nodes[f"gcp:project:{KEYS}"].parent, aw.id)
        self.assertEqual(topo.meta["collector"], "gcp-org")
        self.assertEqual(topo.meta["folders"], sorted([AW_FOLDER, PROD, NONPROD, SHARED]))
        self.assertEqual(topo.meta["projects"], sorted(gcp_fake.NUMBERS))
        self.assertNotIn("warnings", topo.meta)

    def test_labels_on_request(self):
        topo, _ = collect(include=["labels"])
        self.assertEqual(topo.nodes[f"gcp:project:{HOST}"].attrs["labels"], {"team": "network"})

    def test_deleted_projects_are_skipped(self):
        doomed = {"name": "projects/1", "projectId": "doomed", "state": "DELETE_REQUESTED"}
        topo, _ = collect({(f"{CRM}/projects", f"folders/{PROD}"): {"projects": [doomed]}})
        self.assertNotIn("gcp:project:doomed", topo.nodes)

    def test_unlistable_branch_is_a_warning(self):
        topo, _ = collect({(f"{CRM}/folders", f"folders/{SHARED}"): DENIED})
        self.assertNotIn(f"gcp:project:{HOST}", topo.nodes)
        self.assertIn(f"gcp:project:{KEYS}", topo.nodes)
        self.assertTrue(any("Assured Workloads/Shared" in w and "PERMISSION_DENIED" in w
                            for w in topo.meta["warnings"]), topo.meta)

    def test_only_get_requests(self):
        # The transport has no method argument at all: everything is a GET.
        _, fake = collect()
        self.assertTrue(fake.calls)
        self.assertTrue(all(url.startswith("https://") for url, _ in fake.calls))


class OrganizationChoiceTest(unittest.TestCase):
    def test_explicit_organization(self):
        topo, fake = collect({f"{CRM}/organizations:search": DENIED}, organization=f"organizations/{ORG}")
        self.assertIn(f"gcp:organization:{ORG}", topo.nodes)
        self.assertNotIn(f"{CRM}/organizations:search", [u for u, _ in fake.calls])

    def test_none_visible(self):
        with self.assertRaisesRegex(RuntimeError, "can't see any"):
            collect({f"{CRM}/organizations:search": {"organizations": []}})

    def test_several_visible(self):
        two = {"organizations": [{"name": "organizations/1"}, {"name": "organizations/2"}]}
        with self.assertRaisesRegex(RuntimeError, "several organizations"):
            collect({f"{CRM}/organizations:search": two})

    def test_denied(self):
        with self.assertRaisesRegex(RuntimeError, "organizationViewer"):
            collect({f"{CRM}/organizations/{ORG}": DENIED}, organization=ORG)


class AssuredWorkloadsTest(unittest.TestCase):
    def test_workload_marks_its_folder_and_key_project(self):
        topo, _ = collect()
        aw = topo.nodes[f"gcp:folder:{AW_FOLDER}"].attrs["assured_workload"]
        self.assertEqual(aw["compliance_regime"], "FEDRAMP_MODERATE")
        self.assertEqual(aw["location"], "us")
        self.assertEqual(aw["active_violations"], 2)
        self.assertEqual(aw["acknowledged_violations"], 1)
        keys = topo.nodes[f"gcp:project:{KEYS}"].attrs
        self.assertEqual(keys["assured_workload_role"], "keys")
        self.assertEqual(keys["assured_workload"]["name"], "fedramp-mod-1")
        self.assertNotIn("assured_workload", topo.nodes[f"gcp:folder:{PROD}"].attrs)
        self.assertEqual(topo.meta["assured_workloads"], 1)

    def test_listed_locations_and_extra_ones(self):
        listed = {f"{gcp_fake.ASSURED_WORKLOADS}/organizations/{ORG}/locations":
                  {"locations": [{"locationId": "eu"}]}}
        topo, fake = collect(listed, aw_locations=["us"])
        urls = [u for u, _ in fake.calls if u.endswith("/workloads")]
        self.assertEqual([u.split("/")[-2] for u in urls], ["eu", "us"])
        self.assertEqual(topo.meta["assured_workloads"], 1)

    def test_denied_is_one_warning(self):
        url = f"{gcp_fake.ASSURED_WORKLOADS}/organizations/{ORG}/locations/eu/workloads"
        topo, fake = collect({url: DENIED})
        self.assertEqual(sum("Assured Workloads" in w for w in topo.meta["warnings"]), 1)
        self.assertNotIn("assured_workload", topo.nodes[f"gcp:folder:{AW_FOLDER}"].attrs)


class OrgPolicyTest(unittest.TestCase):
    def test_constraints_set_on_each_node(self):
        topo, _ = collect()
        self.assertEqual(topo.nodes[f"gcp:organization:{ORG}"].attrs["org_policies"],
                         ["compute.vmExternalIpAccess", "gcp.resourceLocations"])
        self.assertEqual(topo.nodes[f"gcp:folder:{AW_FOLDER}"].attrs["org_policies"],
                         ["gcp.restrictNonCmekServices"])
        self.assertNotIn("org_policies", topo.nodes[f"gcp:project:{HOST}"].attrs)

    def test_skipped(self):
        topo, fake = collect(policies="none")
        self.assertNotIn("org_policies", topo.nodes[f"gcp:organization:{ORG}"].attrs)
        self.assertFalse([u for u, _ in fake.calls if u.startswith(ORG_POLICY)])

    def test_denied_is_one_warning(self):
        denied = {f"{ORG_POLICY}/organizations/{ORG}/policies": DENIED,
                  f"{ORG_POLICY}/folders/{AW_FOLDER}/policies": DENIED}
        topo, _ = collect(denied, workers=1)
        warnings = [w for w in topo.meta["warnings"] if "org policies" in w]
        self.assertEqual(len(warnings), 1)


class RenderTest(unittest.TestCase):
    def test_assured_folder_stands_out(self):
        topo, _ = collect()
        drawio = render(topo, "drawio")
        self.assertIn("strokeColor=#D93025", drawio)  # the Assured Workloads folder
        self.assertIn("AW: FEDRAMP MODERATE (us) · 2 violations", drawio)
        svg = render(topo, "svg")
        self.assertIn("AW: FEDRAMP MODERATE (us)", svg)
        self.assertIn("subgraph", render(topo, "mermaid"))


class CliTest(unittest.TestCase):
    def test_collect(self):
        api, _ = gcp_fake.api()
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "org.json")
            with mock.patch.object(gcp_org, "api_from_args", lambda args: api):
                status, _, err = run("collect", "gcp-org", "--include", "labels", "-o", out)
            self.assertEqual(status, 0, err)
            topo = Topology.load(out)
            self.assertEqual(topo.meta["organization"], ORG)
            self.assertIn("labels", topo.nodes[f"gcp:project:{HOST}"].attrs)

    def test_bad_include(self):
        err = io.StringIO()
        with redirect_stderr(err), self.assertRaises(SystemExit) as exit_:
            main(["collect", "gcp-org", "--include", "secrets"])
        self.assertEqual(exit_.exception.code, 2)
        self.assertIn("unknown --include value", err.getvalue())


if __name__ == "__main__":
    unittest.main()
