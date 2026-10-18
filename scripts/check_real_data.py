#!/usr/bin/env python3
"""Fail when tracked files look like they hold data from a real network.

The repo rule is that fixtures use made-up values only. This is the cheap
check for the usual slips, run in CI over every tracked text file:

* AWS account IDs (12 digits) that aren't obviously fake;
* AWS Organizations IDs (`o-...`, `ou-...`, `r-...`).

Public IPv4 addresses are deliberately not checked: versions, multicast
groups and the redactor's own examples make that too noisy to be useful.

"Obviously fake" means few distinct digits (`111122223333`), the usual
`123456789012`, what `topomap anonymize` generates (`1000000000NN`,
`o-0000000001`), or an ID containing `example`. Anything else that is
legitimate goes in `scripts/real-data-allowlist.txt`, one exact token per
line (`#` starts a comment).

    python3 scripts/check_real_data.py            # every tracked file
    python3 scripts/check_real_data.py FILE ...   # just these
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Iterable, Iterator, List, Set, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALLOWLIST = os.path.join(ROOT, "scripts", "real-data-allowlist.txt")

ACCOUNT = re.compile(r"(?<![\w.-])\d{12}(?![\w.-])")
ORG_ID = re.compile(r"(?<![\w-])(?:o-[0-9a-z]{10,32}|ou-[0-9a-z]{4,32}-[0-9a-z]{8,32}|r-[0-9a-z]{4,32})(?![\w-])")
BINARY = (".png", ".jpg", ".jpeg", ".gif", ".pdf", ".ico", ".zip", ".gz", ".pcap", ".pcapng")


def fake_account(value: str) -> bool:
    return len(set(value)) <= 4 or value in ("123456789012",) or value.startswith("10000000")


def fake_org_id(value: str) -> bool:
    body = value.split("-", 1)[1]
    return ("example" in value or body.startswith("000")
            or (len(body) >= 8 and len(set(body.replace("-", ""))) <= 4))


def scan_text(text: str) -> Iterator[Tuple[int, str, str]]:
    """(line number, what, token) for each suspicious value in `text`."""
    for number, line in enumerate(text.splitlines(), 1):
        for m in ACCOUNT.finditer(line):
            if not fake_account(m.group(0)):
                yield number, "AWS account ID", m.group(0)
        for m in ORG_ID.finditer(line):
            if not fake_org_id(m.group(0)):
                yield number, "AWS Organizations ID", m.group(0)


def load_allowlist(path: str = ALLOWLIST) -> Set[str]:
    try:
        with open(path, encoding="utf-8") as f:
            lines = [line.split("#", 1)[0].strip() for line in f]
    except FileNotFoundError:
        return set()
    return {line for line in lines if line}


def tracked_files() -> List[str]:
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True)
    return [p for p in out.stdout.decode().split("\0") if p]


def check(paths: Iterable[str], allowed: Set[str]) -> List[str]:
    problems = []
    for path in paths:
        if path.lower().endswith(BINARY):
            continue
        full = path if os.path.isabs(path) else os.path.join(ROOT, path)
        try:
            with open(full, encoding="utf-8") as f:
                text = f.read()
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        for number, what, token in scan_text(text):
            if token not in allowed:
                problems.append(f"{path}:{number}: {what} {token}")
    return problems


def main(argv: List[str]) -> int:
    problems = check(argv or tracked_files(), load_allowlist())
    for problem in problems:
        print(problem)
    if problems:
        print(f"\n{len(problems)} value(s) look like real data. Replace them with made-up ones "
              "(or run `topomap anonymize`), or list a legitimate token in scripts/real-data-allowlist.txt.",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
