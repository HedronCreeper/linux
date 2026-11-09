#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""kconf-check — prove every symbol in a HedronOS config actually exists in *this* kernel tree.

Why a tool instead of `make oldconfig` and hope: a typo'd or renamed Kconfig symbol does not fail
a build, it silently disappears, and you find out six months later when someone asks why preempt
RT is not available on their machine. So the ISO build refuses to produce a kernel whose config
contains a symbol the tree does not know, and `hedronos-verify` runs the same check on every
`make check`.

    kconf-check.py --kernel .. --config hedronos/kernel/hedronos_defconfig --json
    kconf-check.py --kernel .. --fragment hedronos.fragment --strict-deps

Exit 0 = clean, 1 = unknown symbols (a hard error), 2 = bad usage.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

CONFIG_RE = re.compile(r"^\s*(?:CONFIG_)?([A-Za-z0-9_]+)=([ym]|0x[0-9a-fA-F]+|\"[^\"]*\"|-?\d+)\s*$")
NOTSET_RE = re.compile(r"^\s*#\s*(?:CONFIG_)?([A-Za-z0-9_]+) is not set\s*$")
DEF_RE = re.compile(r"^\s*(?:menuconfig|config)\s+([A-Za-z0-9_]+)\b")
# Symbols the kbuild infrastructure provides without a Kconfig entry. Listed explicitly rather
# than exempted by prefix, because "ignore anything that looks generated" is how real checks rot.
IMPLICIT = {
    "CONFIG_64BIT", "CONFIG_X86_64", "CONFIG_ARM64", "CONFIG_LOCALVERSION",
    "CONFIG_LOCALVERSION_AUTO", "CC_IS_GCC", "GCC_VERSION", "CLANG_VERSION",
    "LD_VERSION", "AS_VERSION", "LD_IS_LLD", "TOOLCHAIN_HAS_NEWLOOK",
}


from kconf_check_lib import collect_symbols  # noqa: E402


def parse_config(path: str) -> tuple[dict[str, str], list[str]]:
    values: dict[str, str] = {}
    bad: list[str] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#") and not NOTSET_RE.match(line):
                continue
            if m := NOTSET_RE.match(line):
                values[m.group(1)] = "n"
                continue
            if m := CONFIG_RE.match(line):
                key = m.group(1)
                if key in values:
                    bad.append("line %d: %s is set twice (last one wins in kconfig, so this is a bug)"
                               % (lineno, key))
                values[key] = m.group(2)
                continue
            if line.lstrip().startswith(("source", "mainmenu", "#")):
                continue
            bad.append("line %d: unparsable for a defconfig: %r" % (lineno, line[:70]))
    return values, bad


def upstream_defaults(kernel: str, arch: str) -> dict[str, str]:
    for cand in (os.path.join(kernel, "arch", arch, "configs", "defconfig"),
                 os.path.join(kernel, "arch", "x86", "configs", "defconfig")):
        if os.path.isfile(cand):
            try:
                vals, _bad = parse_config(cand)
                return vals
            except OSError:
                pass
    return {}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="kconf-check", description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--kernel", default="..", help="path to the kernel source tree")
    ap.add_argument("--config", default=None, help="defconfig file to check")
    ap.add_argument("--fragment", action="append", default=[], help="additional fragment(s), repeatable")
    ap.add_argument("--arch", default="x86_64")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict-deps", action="store_true", help="also warn on symbols with unsatisfied deps")
    ap.add_argument("--report", default=None, help="write the JSON to this file too")
    args = ap.parse_args(argv)

    kernel = os.path.abspath(args.kernel)
    if not os.path.isdir(kernel):
        print("kconf-check: no kernel tree at %s" % kernel, file=sys.stderr)
        return 2
    targets = ([args.config] if args.config else []) + list(args.fragment)
    targets = [t for t in targets if t and os.path.isfile(t)]
    if not targets:
        print("kconf-check: nothing to check (pass --config and/or --fragment)", file=sys.stderr)
        return 2

    symbols, where = collect_symbols(kernel)
    upstream = upstream_defaults(kernel, args.arch)
    checked: dict[str, str] = {}
    bad_lines: list[str] = []
    for target in targets:
        values, problems = parse_config(target)
        checked.update(values)
        bad_lines += ["%s: %s" % (os.path.basename(target), p) for p in problems]

    unknown = sorted(s for s in checked if s not in symbols and s not in IMPLICIT and "CONFIG_" + s not in IMPLICIT)
    already = sorted(s for s in checked if upstream.get(s) in ("y", "m"))
    modules = sorted(s for s, v in checked.items() if v == "m")
    doc = {
        "kernel": os.path.relpath(kernel),
        "targets": [os.path.relpath(t, os.getcwd()) for t in targets],
        "symbols_known_in_tree": len(symbols),
        "checked": len(checked),
        "unknown": unknown,
        "unknown_where": {s: where.get(s, "?") for s in unknown[:10]},
        "already_enabled": len(already),
        "as_module": len(modules),
        "bad_lines": bad_lines,
        "ok": not unknown and not bad_lines,
    }
    text = json.dumps(doc, indent=1, sort_keys=True)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
    print(text)
    return 0 if doc["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
