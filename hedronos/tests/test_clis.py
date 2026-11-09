"""Every CLI we ship into /usr/bin must actually run.  SPDX-License-Identifier: MIT

One generated test per binary, so `Ran NN tests` counts the real surface: 20 CLIs is 20 checks that
someone did not leave a half-written file in a package. A CLI that cannot print `--help` is not a
CLI, and `hedron-doctor` will report it as a failure of the machine rather than of the package.
"""
from __future__ import annotations

import os
import re
import unittest

from helpers import HEDRON_DIR, bin_paths, run_cli

BINS = bin_paths()


def _read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


class _CliMixin:
    """Shared assertions, instantiated once per binary below.

    `__test__ = False` matters: unittest loads every TestCase subclass a module defines, and this
    base class has no binary to run — it would report failures for a file name that is "".
    """

    bin_path = ""
    name = ""

    def test_file_is_not_hollow(self):
        self.assertGreater(os.path.getsize(self.bin_path), 40, "%s is empty" % self.name)
        raw = _read(self.bin_path)
        self.assertNotIn(b"\x00", raw[:4096] if raw[:4] != b"\x7fELF" else b"",
                         "%s looks like a compiled artifact left in the source tree" % self.name)

    def test_shebang_or_binary(self):
        raw = _read(self.bin_path)
        if raw[:4] == b"\x7fELF":
            return                       # built by the package Makefile (see its [build] command)
        self.assertTrue(raw.startswith(b"#!"), "%s has no shebang and is not a binary" % self.name)
        first = raw.split(b"\n", 1)[0].decode("utf-8", "replace")
        self.assertTrue(first.endswith(("python3", "bash", "sh")) or "env " in first,
                        "%s: interpreter line is %r — use `#!/usr/bin/env X`" % (self.name, first))

    def test_executable_bit_is_set(self):
        # A mode-644 /usr/bin/foo is the single most common packaging mistake in this tree, and
        # pacman will happily ship it: the file appears, and `exec` says permission denied.
        self.assertTrue(os.access(self.bin_path, os.X_OK), "%s is not executable (chmod +x it)" % self.name)

    def test_help_exits_zero(self):
        proc = run_cli(self.bin_path, "--help", timeout=60)
        out = proc.stdout + proc.stderr
        if proc.returncode != 0:
            # A compiled tool may print usage on stderr and exit 2, as getopt does. That is still an
            # answer; printing nothing at all, or a traceback, is not.
            self.assertIn("usage", out.lower(),
                          "%s --help exited %d without a usage line: %s"
                          % (self.name, proc.returncode, out.strip()[-200:]))
            return
        self.assertTrue(out.strip(), "%s --help printed nothing" % self.name)
        self.assertNotIn("Traceback", out + proc.stderr)
        self.assertNotIn("usage: -", out, "%s: argparse did not learn its own name" % self.name)

    def test_help_teaches_something(self):
        # "usage: foo" alone is not documentation. A shipped CLI's help must list verbs or options
        # densely enough that a user can act on it — measured as distinct words, because every one of
        # our tools documents itself in a different shape (argparse, a sed'd header, getopt in C).
        proc = run_cli(self.bin_path, "--help", timeout=60)
        text = (proc.stdout + proc.stderr).lower()
        words = set(re.findall(r"\b[a-z][a-z0-9-]{3,}\b", text))
        self.assertGreaterEqual(len(words), 8,
                                "%s: help text has %d words — that is not a manual page" % (self.name, len(words)))
        self.assertTrue(re.search(r"\b(status|list|apply|plan|start|show|usage|query|verify|search|"
                                  r"install|toggle|mode|report|snapshot|next|help|json)\b", text),
                        "%s: help text names no command or option at all" % self.name)


def make_cases():
    for name, path in sorted(BINS.items()):
        case = type("Test_" + name.replace("-", "_").replace(".", "_"), (unittest.TestCase, _CliMixin),
                    {"bin_path": path, "name": name,
                     "__module__": __name__,
                     "__doc__": "usr/bin/%s from %s" % (name, os.path.relpath(path, HEDRON_DIR).split(os.sep)[1])})
        yield case


for _case in make_cases():
    globals()[_case.__name__] = _case

if not BINS:
    class TestNoBinsFound(unittest.TestCase):
        def test_fail_loudly(self):
            self.fail("no CLI found under packages/*/src/usr/bin — the tree is not what this test assumes")

