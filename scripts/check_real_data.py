#!/usr/bin/env python3
"""Fail when the repository holds data from a real network or real secrets.

The repo rule is that fixtures use made-up values only. This is the cheap
check for the usual slips:

* AWS account IDs (12 digits) that aren't obviously fake;
* AWS Organizations IDs (`o-...`, `ou-...`, `r-...`);
* AWS access key IDs (`AKIA...`, `ASIA...`) and secret access keys assigned
  to a name like `aws_secret_access_key`;
* private key blocks and GitHub or Slack tokens.

Public IPv4 addresses are deliberately not checked: versions, multicast
groups and the redactor's own examples make that too noisy to be useful.

"Obviously fake" means few distinct digits (`111122223333`), the usual
`123456789012`, what `topomap anonymize` generates (`100000NNNNNN`,
`o-0000000001`), or a value containing `EXAMPLE`. Anything else that is
legitimate goes in `scripts/real-data-allowlist.txt`, one exact token per
line (`#` starts a comment).

    python3 scripts/check_real_data.py            # tracked and new, unignored files
    python3 scripts/check_real_data.py FILE ...   # just these
    python3 scripts/check_real_data.py --staged   # lines about to be committed
    python3 scripts/check_real_data.py --history  # every line ever added, all refs

Files that aren't UTF-8 text are listed, not silently ignored; they only fail
the check with `--fail-on-skipped`. Install the pre-commit hook with
`git config core.hooksPath scripts/hooks`.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from typing import Iterable, Iterator, List, Optional, Set, Tuple

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ALLOWLIST = os.path.join(ROOT, "scripts", "real-data-allowlist.txt")

ACCOUNT = re.compile(r"(?<![\w.-])\d{12}(?![\w.-])")
ORG_ID = re.compile(r"(?<![\w-])(?:o-[0-9a-z]{10,32}|ou-[0-9a-z]{4,32}-[0-9a-z]{8,32}"
                    r"|r-[0-9a-z]{4,32})(?![\w-])")
ACCESS_KEY = re.compile(r"(?<![A-Za-z0-9])(?:AKIA|ASIA|AGPA|AIDA|AROA|ANPA|ANVA|AIPA)"
                        r"[A-Z0-9]{16}(?![A-Za-z0-9])")
SECRET_KEY = re.compile(r"(?i)(?:aws_?secret_?access_?key|secret_?access_?key)[\"']?\s*[=:]\s*"
                        r"[\"']?([A-Za-z0-9/+=]{40})(?![A-Za-z0-9/+=])")
PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY(?: BLOCK)?-----")
TOKEN = re.compile(r"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{36,}|xox[baprs]-[A-Za-z0-9-]{10,})")
BINARY = (".png", ".jpg", ".jpeg", ".gif", ".pdf", ".ico", ".zip", ".gz", ".pcap", ".pcapng")


def fake_account(value: str) -> bool:
    return (len(set(value)) <= 4 or value == "123456789012"
            or re.fullmatch(r"100000\d{6}", value) is not None)


def fake_org_id(value: str) -> bool:
    body = value.split("-", 1)[1]
    return ("example" in value.lower() or body.startswith("000")
            or (len(body) >= 8 and len(set(body.replace("-", ""))) <= 4))


def scan_text(text: str) -> Iterator[Tuple[int, str, str]]:
    """(line number, what, token) for each suspicious value in `text`."""
    for number, line in enumerate(text.splitlines(), 1):
        yield from scan_line(number, line)


def scan_line(number: int, line: str) -> Iterator[Tuple[int, str, str]]:
    for m in ACCOUNT.finditer(line):
        if not fake_account(m.group(0)):
            yield number, "AWS account ID", m.group(0)
    for m in ORG_ID.finditer(line):
        if not fake_org_id(m.group(0)):
            yield number, "AWS Organizations ID", m.group(0)
    for m in ACCESS_KEY.finditer(line):
        if "EXAMPLE" not in m.group(0):
            yield number, "AWS access key ID", m.group(0)
    for m in SECRET_KEY.finditer(line):
        if "EXAMPLE" not in m.group(1).upper():
            yield number, "AWS secret access key", m.group(1)
    for m in PRIVATE_KEY.finditer(line):
        yield number, "private key block", m.group(0)
    for m in TOKEN.finditer(line):
        yield number, "access token", m.group(0)


def load_allowlist(path: str = ALLOWLIST) -> Set[str]:
    try:
        with open(path, encoding="utf-8") as f:
            lines = [line.split("#", 1)[0].strip() for line in f]
    except FileNotFoundError:
        return set()
    return {line for line in lines if line}


def _git(*args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, check=True).stdout


def tracked_files() -> List[str]:
    """Tracked files plus new ones git doesn't ignore: anything `git add .`
    would pick up."""
    out = _git("ls-files", "-z", "--cached", "--others", "--exclude-standard")
    return sorted({p for p in out.decode().split("\0") if p})


def check(paths: Iterable[str], allowed: Set[str],
          skipped: Optional[List[str]] = None) -> List[str]:
    """Problems in the named files. Files that can't be read as UTF-8 text
    are appended to `skipped` when it is given."""
    problems = []
    for path in paths:
        if path.lower().endswith(BINARY):
            continue
        full = path if os.path.isabs(path) else os.path.join(ROOT, path)
        try:
            with open(full, encoding="utf-8") as f:
                text = f.read()
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            if skipped is not None and os.path.isfile(full):
                skipped.append(path)
            continue
        for number, what, token in scan_text(text):
            if token not in allowed:
                problems.append(f"{path}:{number}: {what} {token}")
    return problems


def scan_diff(diff: str, allowed: Set[str]) -> List[str]:
    """Problems on the added lines of a `git diff` / `git log -p` listing
    (made with `-U0 --format=commit:%H`). Removed lines are ignored: deleting
    a bad value is what we want."""
    problems, commit, path = [], "", ""
    for line in diff.splitlines():
        if line.startswith("commit:"):
            commit = line[7:15]
        elif line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else line[4:]
        elif line.startswith("+") and not line.startswith("+++"):
            for _, what, token in scan_line(0, line[1:]):
                if token not in allowed:
                    where = f"{commit}:" if commit else ""
                    problems.append(f"{where}{path}: {what} {token}")
    return sorted(set(problems))


def check_staged(allowed: Set[str]) -> List[str]:
    return scan_diff(_git("diff", "--cached", "-U0", "--no-color", "--no-ext-diff").decode(
        errors="replace"), allowed)


def check_history(allowed: Set[str]) -> List[str]:
    return scan_diff(_git("log", "--all", "-p", "-U0", "--no-color", "--no-ext-diff",
                          "--format=commit:%H").decode(errors="replace"), allowed)


def main(argv: List[str]) -> int:
    flags = {a for a in argv if a.startswith("--")}
    paths = [a for a in argv if not a.startswith("--")]
    unknown = flags - {"--staged", "--history", "--fail-on-skipped"}
    if unknown or (flags & {"--staged", "--history"} and paths) or \
            {"--staged", "--history"} <= flags:
        print(__doc__, file=sys.stderr)
        return 2
    allowed = load_allowlist()
    skipped: List[str] = []
    if "--staged" in flags:
        problems = check_staged(allowed)
    elif "--history" in flags:
        problems = check_history(allowed)
    else:
        problems = check(paths or tracked_files(), allowed, skipped)
    for problem in problems:
        print(problem)
    if skipped:
        print(f"note: skipped {len(skipped)} file(s) that aren't UTF-8 text: "
              + ", ".join(skipped[:10]) + (" ..." if len(skipped) > 10 else ""), file=sys.stderr)
    if problems:
        print(f"\n{len(problems)} value(s) look like real data or secrets. Replace them with "
              "made-up ones (or run `topomap anonymize`), or list a legitimate token in "
              "scripts/real-data-allowlist.txt.", file=sys.stderr)
        return 1
    return 1 if skipped and "--fail-on-skipped" in flags else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
