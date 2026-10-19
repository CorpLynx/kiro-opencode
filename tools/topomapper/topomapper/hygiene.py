"""Care for output that holds real data.

Snapshots and diagrams carry account IDs, hostnames and addresses from a
real estate, so they are written readable by the owner only. And if one
lands inside a git work tree without being ignored, it's one `git add .`
away from a commit, so say so. `anonymize` output is meant to be shared and
gets neither treatment.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import Optional

F_GETPATH_DARWIN = 50


def untracked_hint(path: str) -> Optional[str]:
    """A message when `path` is inside a git work tree and not ignored by it."""
    if not path or path == "-":
        return None
    folder = os.path.dirname(os.path.abspath(path))
    while not os.path.isdir(folder) and os.path.dirname(folder) != folder:
        folder = os.path.dirname(folder)  # the output folder may not exist yet
    try:
        done = subprocess.run(["git", "check-ignore", "-q", os.path.abspath(path)], cwd=folder,
                              capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 1:  # 0: ignored, 128: not a repository
        return None
    return (f"{path} is inside a git repository and isn't ignored. It holds real account IDs, "
            "names and addresses: keep it out of commits (write it under an ignored folder such "
            "as topomapper-out/) or share a copy made with `topomapper anonymize`")


def write_private(path: str, text: str) -> None:
    """Write `text` to `path` with mode 0600, creating its folder (0700) and
    tightening a file that already exists with looser permissions."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        os.chmod(path, 0o600)
    except OSError:  # a filesystem without Unix modes
        pass
    _hand_back(path)
    if os.stat(folder).st_uid == 0:
        _hand_back(folder)


def _hand_back(path: str) -> None:
    """Under sudo, give the file to the user who ran sudo: output owned by
    root can't be read, rendered or deleted without sudo again."""
    uid, gid = os.environ.get("SUDO_UID", ""), os.environ.get("SUDO_GID", "")
    if hasattr(os, "geteuid") and os.geteuid() == 0 and uid.isdigit() and gid.isdigit():
        try:
            os.chown(path, int(uid), int(gid))
        except OSError:
            pass


def stdout_file() -> Optional[str]:
    """The file standard output is redirected to (`topomapper ... > out.json`),
    when the platform can say; None for a terminal, a pipe or anything else."""
    if sys.stdout is not sys.__stdout__:
        return None
    try:
        if sys.platform == "darwin":
            import fcntl
            raw = fcntl.fcntl(1, F_GETPATH_DARWIN, b"\0" * 1024)
            path = raw.split(b"\0", 1)[0].decode()
        else:
            path = os.readlink("/proc/self/fd/1")
    except (ImportError, OSError, ValueError):
        return None
    return path if path and os.path.isfile(path) else None
