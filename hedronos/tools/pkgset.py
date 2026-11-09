#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Package-set CLI: resolve a profile/variant into a machine-readable install plan.

Used by build.sh (`stage_pkgset_resolve`), by `hedron-doctor`, and by the test suite.
Stdlib only, no network, no root, deterministic output (sorted + stable floats).
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hedronlib.pkgsets import Resolver, dedupe_sources, load_sets  # noqa: E402
from hedronlib.util import dump_json, eprint  # noqa: E402


def cmd_resolve(args: argparse.Namespace) -> int:
    sets, hedron = load_sets(args.data, args.packages)
    res = Resolver(sets, hedron).resolve(args.profile, args.variant, args.arch,
                                         include_default_apps=not args.no_default_apps)
    if args.json:
        dump_json(res.as_dict(), args.json)
    if args.pretty:
        print("profile=%s variant=%s arch=%s" % (res.profile, res.variant, res.arch))
        print("packages: %d   flatpaks: %d   size: %.1f GiB"
              % (len(res.packages), len(res.flatpaks), res.size_mib / 1024))
        for line in Resolver(sets, hedron).size_report(res):
            print("  " + line)
        for note in res.notes:
            print("  note: " + note)
        for note in res.conflicts_applied:
            print("  conflict: " + note)
        for err in res.errors:
            print("  ERROR: " + err, file=sys.stderr)
    if not args.json and not args.pretty:
        sys.stdout.write("\n".join(res.names()) + "\n")
    if args.fail_on_error and res.errors:
        for err in res.errors:
            eprint("error: " + err)
        return 1
    return 0


def cmd_render(args: argparse.Namespace) -> int:
    data = json.load(open(args.json, encoding="utf-8"))
    names = [p["name"] for p in data["packages"]]
    if args.txt:
        with open(args.txt, "w", encoding="utf-8") as fh:
            fh.write("\n".join(names) + "\n")
    if args.by_source:
        grouped = {k: v for k, v in dedupe_sources(
            [type("R", (), {"name": p["name"], "source": p["source"]})() for p in data["packages"]]).items() if v}
        with open(args.by_source, "w", encoding="utf-8") as fh:
            for source in sorted(grouped):
                fh.write("# %s (%d)\n" % (source, len(grouped[source])))
                for name in grouped[source]:
                    fh.write(name + "\n")
    if args.groups:
        with open(args.groups, "w", encoding="utf-8") as fh:
            current = None
            for p in data["packages"]:
                group = p.get("group") or "ungrouped"
                if group != current:
                    fh.write("\n# group: %s\n" % group)
                    current = group
                fh.write(p["name"] + "\n")
    if args.size_report:
        by_group: dict[str, float] = {}
        for p in data["packages"]:
            by_group[p.get("group") or "ungrouped"] = by_group.get(p.get("group") or "ungrouped", 0.0) + p.get("estimate_mib", 0.0)
        with open(args.size_report, "w", encoding="utf-8") as fh:
            for group in sorted(by_group, key=lambda g: -by_group[g]):
                fh.write("%-18s %8.1f MiB\n" % (group, by_group[group]))
            fh.write("%-18s %8.1f MiB\n" % ("TOTAL", data.get("size_mib", 0.0)))
    return 0


def cmd_select(args: argparse.Namespace) -> int:
    data = json.load(open(args.json, encoding="utf-8"))
    for p in data["packages"]:
        if args.source and p.get("source") != args.source:
            continue
        if args.tier and p.get("tier") != args.tier:
            continue
        print(p["name"])
    return 0


def cmd_digest(args: argparse.Namespace) -> int:
    import hashlib
    with open(args.json, "rb") as fh:
        print(hashlib.sha256(fh.read()).hexdigest()[:16])
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    sets, hedron = load_sets(args.data, args.packages)
    r = Resolver(sets, hedron)
    print("profiles: " + ", ".join(r.profile_names()))
    print("groups:   " + ", ".join(r.group_names()))
    print("hedron packages present: " + (", ".join(sorted(hedron)) or "(none — pass --packages)"))
    for name, meta in sorted(hedron.items()):
        print("  %-24s v%-8s %s" % (name, meta.get("pkgver", "?"), meta.get("summary", "")[:58]))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--data", default="data", help="directory with package-sets.toml / apps.toml")
    common.add_argument("--packages", default=None, help="directory with packages/*/hedronpkg.toml")

    p = sub.add_parser("resolve", parents=[common], help="resolve a profile into a plan")
    p.add_argument("--profile", required=True)
    p.add_argument("--variant", default="dvd")
    p.add_argument("--arch", default="x86_64")
    p.add_argument("--json", default=None, help="write the machine-readable plan here")
    p.add_argument("--pretty", action="store_true")
    p.add_argument("--no-default-apps", action="store_true")
    p.add_argument("--fail-on-error", action="store_true", default=True)
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("render", parents=[common], help="render a resolved plan into build manifests")
    p.add_argument("--json", required=True)
    p.add_argument("--txt")
    p.add_argument("--by-source")
    p.add_argument("--groups")
    p.add_argument("--size-report")
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("select", parents=[common], help="print a subset of a plan")
    p.add_argument("--json", required=True)
    p.add_argument("--source")
    p.add_argument("--tier")
    p.set_defaults(func=cmd_select)

    p = sub.add_parser("digest", parents=[common], help="short hash of a plan (cache key)")
    p.add_argument("--json", required=True)
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser("list", parents=[common], help="list profiles/groups/packages")
    p.set_defaults(func=cmd_list)

    args = ap.parse_args(argv)
    if hasattr(args, "packages") and args.packages and not os.path.isdir(args.packages):
        eprint("pkgset: --packages %s is not a directory" % args.packages)
        return 2
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
