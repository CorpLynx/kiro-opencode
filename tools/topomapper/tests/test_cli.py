"""The command structure: flat commands, `-o` by extension, and the old
`collect <source>` form."""

import contextlib
import io
import json
import argparse
import os
import re
import stat
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from topomapper import cli  # noqa: E402
from topomapper.collectors import aws  # noqa: E402
from topomapper.model import Topology  # noqa: E402

MERGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "merge")
AWS_JSON = os.path.join(MERGE, "aws.json")


def fake_aws(args):
    """`topomapper account collect` without AWS: the saved account in the merge fixtures."""
    return Topology.load(AWS_JSON)


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            status = cli.main(list(argv))
        except SystemExit as e:  # argparse
            status = e.code
    return status, out.getvalue(), err.getvalue()


class CommandsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def path(self, name):
        return os.path.join(self.dir.name, name)

    def collect(self, *argv):
        with mock.patch.object(aws, "run", side_effect=fake_aws):
            return run("account", "collect", *argv)

    def test_one_run_writes_the_topology_and_diagrams(self):
        status, out, err = self.collect("network", self.path("net.json"), self.path("net.drawio"),
                                        "--title", "Lab", self.path("net.svg"), self.path("net.mmd"),
                                        "--direction", "LR")
        self.assertEqual(status, 0, err)
        self.assertEqual(out, "")  # nothing on stdout when every output is a file
        with open(self.path("net.json"), encoding="utf-8") as f:
            self.assertTrue(json.load(f)["nodes"])
        drawio = ET.parse(self.path("net.drawio")).getroot()
        self.assertIn("Lab", ET.tostring(drawio, encoding="unicode"))
        svg = ET.parse(self.path("net.svg")).getroot()
        self.assertTrue(svg.tag.endswith("svg"))
        with open(self.path("net.mmd"), encoding="utf-8") as f:
            self.assertIn("flowchart LR", f.read())
        for name in ("net.json", "net.drawio", "net.svg", "net.mmd"):  # real data: owner only
            self.assertEqual(stat.S_IMODE(os.stat(self.path(name)).st_mode), 0o600, name)

    def test_merge_filter_and_draw(self):
        self.collect(self.path("net.json"))
        status, _, err = run("topology", "merge", self.path("net.json"), "--to", self.path("all.svg"),
                             self.path("all.json"))
        self.assertEqual(status, 0, err)
        self.assertTrue(os.path.getsize(self.path("all.svg")))
        self.assertTrue(os.path.getsize(self.path("all.json")))
        status, _, err = run("topology", "filter", self.path("all.json"), "--aws-only",
                             self.path("view.dot"), "--no-link-labels")
        self.assertEqual(status, 0, err)
        with open(self.path("view.dot"), encoding="utf-8") as f:
            self.assertIn("graph topomapper {", f.read())
        status, out, err = run("diagram", "draw", self.path("all.json"), "--format", "mermaid")
        self.assertEqual(status, 0, err)
        self.assertTrue(out.startswith("flowchart TB"))

    def test_bad_words_are_refused_before_collecting(self):
        with mock.patch.object(aws, "run", side_effect=AssertionError("collected")):
            for argv, message in (
                    (("net.png",), "say what to write with the extension: .json for the topology"),
                    (("netwrk",), "netwrk: not a part (network, iam"),
                    (("--bogus",), "unrecognized arguments: --bogus")):
                status, _, err = run("account", "collect", *argv)
                self.assertEqual(status, 2, argv)
                self.assertIn(message, err)
        status, _, err = run("diagram", "draw", "x.json", "x.json")
        self.assertIn("name the diagram format with the extension", err)

    def test_stdout_is_the_default_and_help_lists_the_groups(self):
        status, out, _ = self.collect()
        self.assertEqual(status, 0)
        self.assertTrue(out.startswith("{"))
        status, out, _ = run("--help")
        self.assertEqual(status, 0)
        for name in ("account", "org", "topology", "diagram"):
            self.assertRegex(out, rf"(?m)^    {name}\s")
        status, out, _ = run("topology", "--help")
        for verb in ("merge", "filter", "diff", "anonymize"):  # 3.9 wraps long names onto a line of their own
            self.assertRegex(out, rf"(?m)^    {verb}\s")

    def test_old_commands_are_gone(self):
        for old in ("aws", "aws-org", "merge", "render", "collect"):
            status, _, err = run(old, "--help")
            self.assertEqual(status, 2, old)
            self.assertIn("invalid choice", err)


class ReadmeReferenceTest(unittest.TestCase):
    """The README's command reference lists every flag and word the parser
    takes, so adding one without documenting it fails here."""

    SHARED = ("--direction", "--title", "--no-link-labels")

    def sections(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "README.md")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        reference = text.split("## Command reference", 1)[1].split("\n## ", 1)[0]
        parts = re.split(r"^### ", reference, flags=re.M)[1:]
        out = {}
        for part in parts:
            title, _, body = part.partition("\n")
            m = re.fullmatch(r"`topomapper ([a-z]+ [a-z]+)`", title.strip())
            out[m.group(1) if m else "shared"] = body
        return out

    def commands(self):
        groups = next(a for a in cli.build_parser()._actions
                      if isinstance(a, argparse._SubParsersAction))
        for group, g in groups.choices.items():
            verbs = next(a for a in g._actions if isinstance(a, argparse._SubParsersAction))
            for verb, sub in verbs.choices.items():
                yield f"{group} {verb}", sub

    def test_every_flag_is_documented(self):
        sections = self.sections()
        for name, sub in self.commands():
            self.assertIn(name, sections, f"no README section for topomapper {name}")
            for action in sub._actions:
                longs = [o for o in action.option_strings if o.startswith("--") and o != "--help"]
                for flag in longs:
                    where = sections["shared"] if (flag in self.SHARED and name != "diagram draw") \
                        else sections[name]
                    self.assertRegex(where, rf"`[^`\n]*(?<![\w-]){re.escape(flag)}[ `]",
                                     f"topomapper {name} {flag} isn't in the README's reference")

    def test_every_word_is_documented(self):
        sections = self.sections()
        from topomapper.collectors import aws_org
        for name, words in (("account collect", [*aws.PARTS, *aws.EXTRAS]),
                            ("org collect", [*aws_org.EXTRAS, *aws_org.POLICY_VIEWS])):
            for word in words:
                self.assertIn(f"`{word}`", sections[name], f"topomapper {name} {word}")


if __name__ == "__main__":
    unittest.main()
