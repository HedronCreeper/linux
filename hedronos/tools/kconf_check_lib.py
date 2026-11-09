"""Shared helper for kconf-check.py and gen-defconfig.py. SPDX-License-Identifier: MIT

The kernel tree's Kconfig files are the only authority on which symbols exist. Both tools need
that set, and duplicating the walk (and its skip list) is how one of them starts disagreeing with
the other.
"""

from __future__ import annotations

import os
import re

DEF_RE = re.compile(r"^\s*(?:menuconfig|config)\s+([A-Za-z0-9_]+)\b")


def collect_symbols(kernel: str) -> tuple[set[str], dict[str, str]]:
    """Every `config`/`menuconfig` name in the tree, plus the Kconfig file that defined it."""
    found: set[str] = set()
    where: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(kernel, followlinks=False):
        rel = os.path.relpath(dirpath, kernel)
        top = rel.split(os.sep)[0]
        # Documentation/ and samples/ hold no Kconfig worth reading; skipping them is the
        # difference between a two-second check and a two-minute one.
        if top in ("Documentation", "samples", "rust") and "Kconfig" not in filenames:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d not in (".git", "work-test", "hedronos")]
        for name in filenames:
            if name != "Kconfig" and not name.startswith("Kconfig."):
                continue
            path = os.path.join(dirpath, name)
            try:
                with open(path, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            for line in text.splitlines():
                m = DEF_RE.match(line)
                if m:
                    sym = m.group(1)
                    found.add(sym)
                    where.setdefault(sym, os.path.relpath(path, kernel))
    return found, where
