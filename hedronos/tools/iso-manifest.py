#!/usr/bin/env python3
"""iso-manifest — inspect or lint the rehearsal's manifest.  SPDX-License-Identifier: MIT

`build.sh --dry-run` writes work/<…>/manifest/iso-manifest.json describing what would go on the
disc. This tool answers two questions about it, which is what a reviewer actually asks:

    tools/iso-manifest.py show   --root .            # human summary of the last rehearsal
    tools/iso-manifest.py lint --root .              # the manifest is self-consistent
    tools/iso-manifest.py lint --root . --json       # machine-readable

`lint` is a separate gate from `hedronos-verify`: the verifier proves the *tree* is coherent, this
proves the *build's decisions* are coherent (a package set that dropped a layer package, a driver
prefetch list that is empty, a kernel config that is not the generated one).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hedronlib.util import sha256_text  # noqa: E402

REQUIRED = ("packages", "driver_prefetch")


def find_manifest(root: str, explicit: str | None) -> str:
    if explicit:
        return explicit
    cand = [os.path.join(root, "work", "dryrun", "manifest", "iso-manifest.json"),
            os.path.join(root, "work", "manifest", "iso-manifest.json")]
    for dirpath, dirnames, filenames in os.walk(os.path.join(root, "work")):
        if "iso-manifest.json" in filenames:
            cand.insert(0, os.path.join(dirpath, "iso-manifest.json"))
        dirnames[:] = [d for d in dirnames if d not in ("packages", "rootfs", "efiboot")]
    for path in cand:
        if os.path.isfile(path):
            return path
    return ""


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def cmd_show(args: argparse.Namespace) -> int:
    path = find_manifest(args.root, args.manifest)
    if not path:
        print("iso-manifest: no manifest yet — run  ./build.sh --dry-run --kernel host", file=sys.stderr)
        return 1
    doc = load(path)
    os_info = doc.get("os", {})
    print("manifest : %s" % os.path.relpath(path, args.root))
    print("product  : %s %s '%s' (%s/%s) build %s" % (os_info.get("name"), os_info.get("version"),
                                                      os_info.get("profile"), os_info.get("variant"),
                                                      os_info.get("build_id"), os_info.get("name")))
    print("packages : %d" % len(doc.get("packages", [])))
    print("drivers  : %d pre-cached package(s)" % len(doc.get("driver_prefetch", [])))
    for name, digest in sorted(doc.get("sha256", {}).items()):
        size = doc.get("sizes_bytes", {}).get(name, 0)
        print("  %-26s %s  %s" % (name, digest[:12], "%d KiB" % (size // 1024) if size else "-"))
    return 0


def cmd_lint(args: argparse.Namespace) -> int:
    problems: list[str] = []
    path = find_manifest(args.root, args.manifest)
    if not path:
        problems.append("no iso-manifest.json under %s/work (run ./build.sh --dry-run)" % args.root)
        doc: dict = {}
    else:
        doc = load(path)
        if doc.get("format") != "hedron-iso-manifest":
            problems.append("format is %r, expected 'hedron-iso-manifest'" % doc.get("format"))
        for key in REQUIRED:
            if not doc.get(key):
                problems.append("%s is empty — the image would install without it" % key)
        names = set(doc.get("packages", []))
        pkgdir = os.path.join(args.root, "packages")
        if os.path.isdir(pkgdir) and names:
            for name in sorted(os.listdir(pkgdir)):
                if not os.path.isfile(os.path.join(pkgdir, name, "hedronpkg.toml")):
                    continue
                if name.startswith("linux-"):
                    continue          # provided by a repo/kernel package, not by the layer sets
                if name not in names:
                    problems.append("packages/%s is not in the resolved package set" % name)
        # The kernel config the image claims to carry must be the generated one.
        gen = os.path.join(args.root, "kernel", "hedronos_defconfig")
        if os.path.isfile(gen) and os.path.isfile(gen + ".sha256"):
            want = open(gen + ".sha256", encoding="utf-8").read().split()[0]
            if sha256_text(open(gen, encoding="utf-8").read()) != want:
                problems.append("kernel/hedronos_defconfig does not match its .sha256 sidecar "
                                "(regenerate with tools/gen-defconfig.py)")
        elif os.path.isfile(gen):
            problems.append("kernel/hedronos_defconfig has no .sha256 sidecar")
        for pkg in doc.get("driver_prefetch", []):
            if not re.match(r"^[a-z0-9][a-z0-9._+-]*$", pkg):
                problems.append("driver_prefetch entry is not a package name: %r" % pkg)
    result = {"manifest": path or None, "ok": not problems, "problems": problems,
              "packages": len(doc.get("packages", [])), "driver_packages": len(doc.get("driver_prefetch", []))}
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    elif problems:
        print("iso-manifest: %d problem(s)" % len(problems), file=sys.stderr)
        for p in problems:
            print("  - %s" % p, file=sys.stderr)
    else:
        print("iso-manifest: OK (%d packages, %d driver package(s))"
              % (result["packages"], result["driver_packages"]))
    return 0 if not problems else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="iso-manifest", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, help_text in (("show", "summarise the last rehearsal"), ("lint", "check it is self-consistent")):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("--root", default=".", help="the hedronos/ directory")
        sp.add_argument("--manifest", default=None, help="explicit manifest path")
        sp.add_argument("--json", action="store_true", help="machine-readable output (lint)")
        sp.set_defaults(func=cmd_show if name == "show" else cmd_lint)
    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
