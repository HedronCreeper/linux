#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Emit the GPU plan set that is baked into the ISO (and used by --explain later).

The build calls this once per profile; the installed system calls the same engine with
the real PCI ids.  Because both paths are the same code, "what the installer promised"
and "what the system did" are the same object, which is what lets hedron-doctor diff
them later and tell you when something drifted.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from hedronlib import drivers, gpus  # noqa: E402
from hedronlib.util import load_json, load_toml  # noqa: E402


def build(db_path: str, rules_path: str, version: str, build_id: str,
          profiles: tuple[str, ...] = ("x86_64",)) -> dict:
    db = gpus.GpuDatabase(load_json(db_path))
    rules = load_toml(rules_path)
    plans = drivers.plan_all_families(db, rules)
    machine = {
        "format": drivers.PLAN_FORMAT,
        "hedronos_version": version,
        "build_id": build_id,
        "db_schema": db.schema,
        "db_generated_from": db.generated,
        "plan_count": len(plans),
        "profiles": list(profiles),
        "plans": plans,
    }
    return machine


def summarize(machine: dict) -> list[str]:
    lines = []
    for plan in machine.get("plans", []):
        lines.append("%-8s %-22s kmod=%-9s branch=%-20s pkgs=%2d params=%s" % (
            plan.get("vendor", "?"),
            plan.get("family", "?"),
            plan.get("kmod", "-"),
            plan.get("branch", "-"),
            len(plan.get("packages", [])),
            " ".join(plan.get("cmdline", [])) or "-",
        ))
    return lines


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", required=True, help="gpu-db.json")
    ap.add_argument("--rules", required=True, help="data/driver-rules.toml")
    ap.add_argument("--out", required=True, help="where to write gpu-plans.json")
    ap.add_argument("--summary", default=None, help="also write a human summary here")
    ap.add_argument("--version", default="0.0.0")
    ap.add_argument("--build-id", default="unknown")
    ap.add_argument("--print", action="store_true", dest="do_print")
    args = ap.parse_args(argv)

    machine = build(args.db, args.rules, args.version, args.build_id)
    text = json.dumps(machine, indent=1, sort_keys=True) + "\n"
    os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(text)
    if args.summary:
        os.makedirs(os.path.dirname(os.path.abspath(args.summary)) or ".", exist_ok=True)
        with open(args.summary, "w", encoding="utf-8") as fh:
            fh.write("\n".join(summarize(machine)) + "\n")
    if args.do_print:
        sys.stdout.write(text)
    print("gpu-plans: %d plans, %d device rows in db → %s"
          % (machine["plan_count"], len(db_rows(args.db)), args.out))
    return 0


def db_rows(path: str) -> list[dict]:
    data = load_json(path)
    out = []
    for key in ("amd", "intel", "nvidia"):
        out.extend(data.get(key, []))
    return out


if __name__ == "__main__":
    raise SystemExit(main())
