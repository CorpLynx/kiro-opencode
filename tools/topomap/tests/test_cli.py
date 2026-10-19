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
from topomap import cli  # noqa: E402
from topomap.collectors import files  # noqa: E402

DEVICES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "devices")


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

    def test_flat_command_and_old_form_agree(self):
        status, flat, err = run("files", DEVICES)
        self.assertEqual(status, 0, err)
        status, old, err = run("collect", "files", DEVICES)
        self.assertEqual(status, 0, err)
        strip = lambda text: {k: v for k, v in json.loads(text).items() if k != "meta"}  # noqa: E731
        self.assertEqual(strip(flat), strip(old))
        self.assertTrue(json.loads(flat)["nodes"])

    def test_one_run_writes_the_topology_and_diagrams(self):
        status, out, err = run("files", DEVICES, "-o", self.path("net.json"),
                               "-o", self.path("net.drawio"), "-o", self.path("net.svg"),
                               "-o", self.path("net.mmd"), "--title", "Lab", "--direction", "LR")
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

    def test_merge_and_filter_draw_too(self):
        run("files", DEVICES, "-o", self.path("net.json"))
        status, _, err = run("merge", self.path("net.json"), "-o", self.path("all.svg"))
        self.assertEqual(status, 0, err)
        self.assertTrue(os.path.getsize(self.path("all.svg")))
        status, _, err = run("filter", self.path("net.json"), "--network-only",
                             "-o", self.path("view.dot"), "--no-link-labels")
        self.assertEqual(status, 0, err)
        with open(self.path("view.dot"), encoding="utf-8") as f:
            self.assertIn("graph topomap {", f.read())

    def test_unknown_extension_is_refused_before_collecting(self):
        with mock.patch.object(files, "run", side_effect=AssertionError("collected")):
            status, _, err = run("files", DEVICES, "-o", self.path("net.png"))
        self.assertEqual(status, 2)
        self.assertIn("say what to write with the extension: .json for the topology", err)

    def test_stdout_still_default_and_help_lists_flat_commands(self):
        status, out, _ = run("files", DEVICES)
        self.assertEqual(status, 0)
        self.assertTrue(out.startswith("{"))
        status, out, _ = run("--help")
        self.assertEqual(status, 0)
        for name in ("aws", "aws-org", "files", "devices", "merge", "render"):
            self.assertIn(f"    {name} ", out)
        self.assertNotIn("collect", out)
        self.assertNotIn("SUPPRESS", out)


class ReadmeReferenceTest(unittest.TestCase):
    """The README's command reference lists every flag the parser has, so
    adding a flag without documenting it fails here."""

    SHARED = ("--output", "--direction", "--title", "--no-link-labels")

    def sections(self):
        path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "README.md")
        with open(path, encoding="utf-8") as f:
            text = f.read()
        reference = text.split("## Command reference", 1)[1].split("\n## ", 1)[0]
        parts = re.split(r"^### ", reference, flags=re.M)[1:]
        out = {}
        for part in parts:
            title, _, body = part.partition("\n")
            m = re.fullmatch(r"`topomap ([a-z-]+)`", title.strip())
            out[m.group(1) if m else "shared"] = body
        return out

    def test_every_flag_is_documented(self):
        sections = self.sections()
        parser = cli.build_parser()
        commands = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        for name, sub in commands.choices.items():
            if name == "collect":
                continue
            self.assertIn(name, sections, f"no README section for topomap {name}")
            for action in sub._actions:
                longs = [o for o in action.option_strings if o.startswith("--") and o != "--help"]
                for flag in longs:
                    where = sections["shared"] if (flag in self.SHARED and name != "render") \
                        else sections[name]
                    self.assertRegex(where, rf"`[^`\n]*(?<![\w-]){re.escape(flag)}[ `]",
                                     f"topomap {name} {flag} isn't in the README's reference")


if __name__ == "__main__":
    unittest.main()
