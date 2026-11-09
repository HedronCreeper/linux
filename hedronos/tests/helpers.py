"""Shared plumbing for the HedronOS unit tests.  SPDX-License-Identifier: MIT

These tests run from a source checkout, not from an installed system: everything is invoked with
explicit paths and temporary state directories. Two rules keep that honest:

  * no test writes outside `tempfile.mkdtemp()` and the repo's own generated files, and
  * no test needs root, a network, pacman, systemd, or a GPU.

If a test needs one of those, it is not a unit test — put it behind `hedronos-verify`'s build
group or write it as a stage in the rehearsal instead.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
HEDRON_DIR = os.path.dirname(TESTS_DIR)
TOOLS_DIR = os.path.join(HEDRON_DIR, "tools")
DATA_DIR = os.path.join(HEDRON_DIR, "data")
PACKAGES_DIR = os.path.join(HEDRON_DIR, "packages")
KERNEL_DIR = os.path.join(HEDRON_DIR, "kernel")
# The repo root is a kernel tree: hedronos/ lives inside linux/.
REPO_ROOT = os.path.dirname(HEDRON_DIR)

if TOOLS_DIR not in sys.path:
    sys.path.insert(0, TOOLS_DIR)


def tool(name: str) -> str:
    return os.path.join(TOOLS_DIR, name)


def kernel_tree() -> str:
    """Path to the kernel tree, or "" when running outside a checkout."""
    return REPO_ROOT if os.path.isfile(os.path.join(REPO_ROOT, "Kconfig")) else ""


def require_kernel(test: unittest.TestCase) -> str:
    ktree = kernel_tree()
    if not ktree:
        test.skipTest("no kernel tree above hedronos/")
    return ktree


def bin_paths() -> dict[str, str]:
    """Every CLI any package installs into /usr/bin, mapped by name."""
    out: dict[str, str] = {}
    for pkg in sorted(os.listdir(PACKAGES_DIR)):
        bindir = os.path.join(PACKAGES_DIR, pkg, "src", "usr", "bin")
        if not os.path.isdir(bindir):
            continue
        for name in sorted(os.listdir(bindir)):
            out.setdefault(name, os.path.join(bindir, name))
    return out


def run_cli(path: str, *args: str, env: dict | None = None, cwd: str | None = None,
            timeout: int = 60) -> subprocess.CompletedProcess:
    """Execute a shipped file the way the installed system would: exec it and let its shebang work.

    Prefixing `python3` here — the obvious shortcut — turns every bash script and every compiled
    tool into a SyntaxError from the interpreter, and the failure looks like a bug in the tool.
    """
    full = dict(os.environ)
    full.update(env or {})
    full.setdefault("PYTHONPATH", TOOLS_DIR)
    full.setdefault("NO_COLOR", "1")
    argv = [path]
    if os.access(path, os.X_OK):
        pass
    else:                                   # not executable in the checkout: pick the right runner
        with open(path, "rb") as fh:
            first = fh.readline()
        argv = ([sys.executable] if b"python" in first else ["bash"]) + [path]
    return subprocess.run(argv + list(args), capture_output=True, text=True,
                          cwd=cwd or HEDRON_DIR, env=full, timeout=timeout)


def run_tool(name: str, *args: str, timeout: int = 600, cwd: str | None = None) -> subprocess.CompletedProcess:
    full = dict(os.environ)
    full.setdefault("PYTHONPATH", TOOLS_DIR)
    return subprocess.run([sys.executable, tool(name), *args], capture_output=True, text=True,
                          cwd=cwd or HEDRON_DIR, env=full, timeout=timeout)


class TempDirMixin:
    """A per-test scratch directory that is always removed."""

    def mktemp(self) -> str:
        if not hasattr(self, "_tmpdirs"):
            self._tmpdirs = []
            self.addCleanup(self._cleanup_tmpdirs)
        d = tempfile.mkdtemp(prefix="hedron-test-")
        self._tmpdirs.append(d)
        return d

    def _cleanup_tmpdirs(self) -> None:
        for d in getattr(self, "_tmpdirs", []):
            shutil.rmtree(d, ignore_errors=True)

    def write(self, relpath: str, text: str, root: str | None = None) -> str:
        root = root or self.mktemp()
        path = os.path.join(root, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        if not hasattr(self, "_last_written_root"):
            self._last_written_root = root
        return path


def json_out(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
