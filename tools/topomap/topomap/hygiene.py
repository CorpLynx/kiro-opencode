"""A reminder when real data is written somewhere git would pick it up.

Snapshots and diagrams carry account IDs, hostnames and addresses from a
real estate. If one lands inside a git work tree without being ignored, it's
one `git add .` away from a commit, so say so. `anonymize` output is meant
to be shared and never triggers this.
"""

from __future__ import annotations

import os
import subprocess
from typing import Optional


def untracked_hint(path: str) -> Optional[str]:
    """A message when `path` is inside a git work tree and not ignored by it."""
    if not path or path == "-":
        return None
    folder = os.path.dirname(os.path.abspath(path))
    try:
        done = subprocess.run(["git", "check-ignore", "-q", os.path.abspath(path)], cwd=folder,
                              capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 1:  # 0: ignored, 128: not a repository
        return None
    return (f"{path} is inside a git repository and isn't ignored. It holds real account IDs, "
            "names and addresses: keep it out of commits (write it under an ignored folder such "
            "as topomap-out/) or share a copy made with `topomap anonymize`")
