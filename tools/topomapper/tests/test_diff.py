import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from topomapper import cli  # noqa: E402
from topomapper.diff import diff  # noqa: E402
from topomapper.model import Topology  # noqa: E402

VPC = "aws:vpc:vpc-0a1"
CORE, EDGE = "aws:transit-gateway:tgw-core1", "aws:vpc:vpc-0e1"


def snapshot():
    t = Topology({"collector": "test", "collected_at": "2026-01-01T00:00:00Z"})
    t.add_node("aws:account:123456789012", "account", "acct")
    t.add_node(VPC, "vpc", "prod (vpc-0a1)", "aws:account:123456789012",
               cidrs=["10.10.0.0/16", "10.11.0.0/16"], tags={"Name": "prod", "Owner": "net"})
    t.add_node(CORE, "transit-gateway", "core1", asn=64512, sources=["exports/core1.json"],
               route_tables=[{"id": "tgw-rtb-1", "routes": ["10.0.0.0/30"], "state": "available"},
                             {"id": "tgw-rtb-2", "routes": ["10.20.10.0/24"], "state": "available"}])
    t.add_node(EDGE, "vpc", "edge (vpc-0e1)", cidrs=["10.0.0.0/30"])
    t.add_link("tgw-attachment", CORE, EDGE, attachment_id="tgw-attach-1",
               subnet_ids=["subnet-a", "subnet-b"])
    return t


def attachment(t):
    return next(l for l in t.links.values() if l.kind == "tgw-attachment")


def run_cli(argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        status = cli.main(argv)
    return status, out.getvalue(), err.getvalue()


class DiffTest(unittest.TestCase):
    def test_identical(self):
        result = diff(snapshot(), snapshot())
        self.assertTrue(result.identical)
        self.assertEqual(result.summary_line(), "no differences")

    def test_meta_and_sources_ignored_by_default(self):
        new = snapshot()
        new.meta["collected_at"] = "2026-02-01T00:00:00Z"
        new.warn("region eu-west-1: AccessDenied")
        new.nodes[CORE].attrs["sources"] = ["exports/core1-new.json"]
        self.assertTrue(diff(snapshot(), new).identical)
        self.assertFalse(diff(snapshot(), new, ignore_attrs=()).identical)

    def test_added_and_removed(self):
        new = snapshot()
        del new.nodes[EDGE]
        del new.links[attachment(new).id]
        new.add_node("aws:vpc:vpc-0b2", "vpc", "staging (vpc-0b2)", "aws:account:123456789012")
        new.add_link("vpc-peering", VPC, "aws:vpc:vpc-0b2", key="pcx-1", peering_id="pcx-1")
        result = diff(snapshot(), new)
        self.assertEqual(result.nodes.added, ["aws:vpc:vpc-0b2"])
        self.assertEqual(result.nodes.removed, [EDGE])
        self.assertEqual(result.links.added, ["vpc-peering|aws:vpc:vpc-0a1|aws:vpc:vpc-0b2|pcx-1"])
        self.assertEqual(result.links.removed, [attachment(snapshot()).id])
        self.assertEqual(result.counts()["nodes"], {"added": 1, "removed": 1, "changed": 0})

    def test_changed_fields_and_attributes(self):
        new = snapshot()
        node = new.nodes[CORE]
        node.label, node.parent = "core1-new", "aws:account:123456789012"
        node.attrs["asn"] = 64513
        new.nodes[VPC].attrs["tags"]["Owner"] = "platform"
        changes = {c.path: c for item in diff(snapshot(), new).nodes.changed for c in item.changes}
        self.assertEqual((changes["label"].old, changes["label"].new), ("core1", "core1-new"))
        self.assertEqual(str(changes["parent"]), "parent: (none) → aws:account:123456789012")
        self.assertEqual(changes["asn"].new, 64513)
        self.assertEqual((changes["tags.Owner"].old, changes["tags.Owner"].new), ("net", "platform"))
        self.assertNotIn("tags.Name", changes)

    def test_lists_are_sets(self):
        new = snapshot()
        new.nodes[VPC].attrs["cidrs"] = ["10.11.0.0/16", "10.10.0.0/16"]
        attachment(new).attrs["subnet_ids"] = ["subnet-b", "subnet-a"]
        self.assertTrue(diff(snapshot(), new).identical)
        new.nodes[VPC].attrs["cidrs"] = ["10.11.0.0/16", "10.12.0.0/16"]
        change = diff(snapshot(), new).nodes.changed[0].changes[0]
        self.assertEqual((change.path, change.added, change.removed),
                         ("cidrs", ["10.12.0.0/16"], ["10.10.0.0/16"]))
        self.assertEqual(str(change), "cidrs: +10.12.0.0/16, -10.10.0.0/16")

    def test_named_list_items_are_matched_by_name(self):
        new = snapshot()
        tables = new.nodes[CORE].attrs["route_tables"]
        tables.reverse()
        tables[0]["state"] = "deleting"
        tables[1]["routes"].append("10.0.0.8/30")
        paths = [str(c) for c in diff(snapshot(), new).nodes.changed[0].changes]
        self.assertEqual(paths, ["route_tables[tgw-rtb-1].routes: +10.0.0.8/30",
                                 "route_tables[tgw-rtb-2].state: available → deleting"])

    def test_ignore_attr_by_name_and_path(self):
        new = snapshot()
        new.nodes[CORE].attrs["route_tables"][0]["state"] = "deleting"
        new.nodes[CORE].attrs["asn"] = 64513
        new.nodes[VPC].attrs["tags"]["Owner"] = "platform"
        result = diff(snapshot(), new, ignore_attrs=("sources", "state", "asn", "tags.Owner"))
        self.assertTrue(result.identical)
        result = diff(snapshot(), new, ignore_attrs=("route_tables.state",))
        self.assertEqual(sorted(i.id for i in result.nodes.changed), [CORE, VPC])

    def test_link_changes(self):
        new = snapshot()
        attachment(new).attrs["subnet_ids"] = ["subnet-b"]
        attachment(new).label = "uplink"
        result = diff(snapshot(), new)
        self.assertEqual(len(result.links.changed), 1)
        self.assertEqual([c.path for c in result.links.changed[0].changes], ["label", "subnet_ids"])

    def test_null_and_missing_are_the_same(self):
        new = snapshot()
        new.nodes[EDGE].attrs["ipv6_cidrs"] = None
        self.assertTrue(diff(snapshot(), new).identical)

    def test_text_format(self):
        new = snapshot()
        del new.nodes[EDGE]
        del new.links[attachment(new).id]
        new.add_node("aws:vpc:vpc-0b2", "vpc", "staging (vpc-0b2)")
        new.nodes[CORE].attrs["asn"] = 64513
        text = diff(snapshot(), new).format_text()
        self.assertEqual(text.splitlines()[0],
                         "nodes: 1 added, 1 removed, 1 changed; links: 0 added, 1 removed, 0 changed")
        for expected in ("Added nodes:\n  + vpc staging (vpc-0b2)  [aws:vpc:vpc-0b2]",
                         "Removed nodes:\n  - vpc edge (vpc-0e1)  [aws:vpc:vpc-0e1]",
                         "Removed links:\n  - tgw-attachment core1 ↔ edge (vpc-0e1)  [tgw-attachment|",
                         "Changed nodes:\n  ~ transit-gateway core1  [aws:transit-gateway:tgw-core1]\n"
                         "      asn: 64512 → 64513"):
            self.assertIn(expected, text)
        order = [text.index(h) for h in ("Added nodes", "Removed nodes", "Removed links",
                                         "Changed nodes")]
        self.assertEqual(order, sorted(order))
        self.assertEqual(diff(snapshot(), new).format_text(summary=True), text.splitlines()[0] + "\n")

    def test_json_format(self):
        new = snapshot()
        new.nodes[CORE].attrs["asn"] = 64513
        new.nodes[CORE].attrs["description"] = "core"
        new.add_node("aws:transit-gateway:tgw-dist1", "transit-gateway", "dist1")
        d = json.loads(json.dumps(diff(snapshot(), new).to_dict()))
        self.assertFalse(d["identical"])
        self.assertEqual(d["nodes"]["added"], [{"id": "aws:transit-gateway:tgw-dist1",
                                                "kind": "transit-gateway", "label": "dist1"}])
        self.assertEqual(d["nodes"]["changed"], [{"id": CORE, "changes": [
            {"path": "asn", "old": 64512, "new": 64513},
            {"path": "description", "new": "core"}]}])
        summary = diff(snapshot(), new).to_dict(summary=True)
        self.assertEqual(set(summary), {"identical", "counts"})


class DiffCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def save(self, name, topo):
        path = os.path.join(self.tmp.name, name)
        topo.save(path)
        return path

    def test_exit_status_and_formats(self):
        old = self.save("old.json", snapshot())
        same = self.save("same.json", snapshot())
        changed = snapshot()
        changed.nodes[CORE].attrs["asn"] = 64513
        new = self.save("new.json", changed)

        status, out, _ = run_cli(["topology", "diff", old, same])
        self.assertEqual((status, out), (0, "no differences\n"))
        status, out, _ = run_cli(["topology", "diff", old, new])
        self.assertEqual(status, 1)
        self.assertIn("asn: 64512 → 64513", out)
        status, out, _ = run_cli(["topology", "diff", old, new, "--format", "json", "--summary"])
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(out)["counts"]["nodes"]["changed"], 1)
        status, out, _ = run_cli(["topology", "diff", old, new, "--summary"])
        self.assertEqual(out.count("\n"), 1)
        status, _, _ = run_cli(["topology", "diff", old, new, "--ignore-attr", "asn"])
        self.assertEqual(status, 0)

    def test_errors_exit_2(self):
        old = self.save("old.json", snapshot())
        status, _, err = run_cli(["topology", "diff", old, os.path.join(self.tmp.name, "missing.json")])
        self.assertEqual(status, 2)
        self.assertIn("topomapper: error:", err)
        bad = os.path.join(self.tmp.name, "bad.json")
        with open(bad, "w", encoding="utf-8") as f:
            f.write("{not json")
        status, _, err = run_cli(["topology", "diff", old, bad])
        self.assertEqual(status, 2)
        self.assertIn("bad.json", err)


if __name__ == "__main__":
    unittest.main()
