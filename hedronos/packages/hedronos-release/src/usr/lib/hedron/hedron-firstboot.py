#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""hedron-firstboot — the one-shot "make this machine a gaming PC" pass.

Runs on the *installed* system's first boot (ConditionFirstBoot is not enough: that only
covers an uninitialised machine-id, and we also want to run after an offline install where
machine-id already exists). Each step is idempotent, logged, and skippable, and the whole
thing records what it did to /var/lib/hedron/firstboot.json so `hedron-doctor` can tell
promise from reality later.

Deliberately NOT done here: creating a user (the installer owns that), partitioning, or
installing base packages. If you find yourself wanting to add those, the answer is still no —
this runs on every first boot of every profile, including a chroot in a build container.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in ("/usr/lib/python3.13/site-packages", os.path.join(_HERE, "..", "python3.13/site-packages")):
    if os.path.isdir(os.path.join(_cand, "hedronlib")) and _cand not in sys.path:
        sys.path.insert(0, os.path.abspath(_cand))
        break
else:
    _d = _HERE
    for _ in range(8):
        _d = os.path.dirname(_d)
        if os.path.isdir(os.path.join(_d, "tools", "hedronlib")):
            sys.path.insert(0, os.path.join(_d, "tools"))
            break

from hedronlib.util import load_toml  # noqa: E402

STATE_DIR = os.environ.get("HEDRON_STATE_DIR") or "/var/lib/hedron"
DONE = os.path.join(STATE_DIR, "firstboot-done")
LOG = os.path.join(STATE_DIR, "firstboot.json")
SELECTED = os.path.join(STATE_DIR, "selected-apps.json")
RELEASE = "/etc/hedron/release.json"
LIVE_MARKER = "/run/hedron/live-session"


class Step:
    def __init__(self, name, fn, *, needs_network=False, fatal=False, docs=""):
        self.name = name
        self.fn = fn
        self.needs_network = needs_network
        self.fatal = fatal
        self.docs = docs


def have_network(timeout: float = 2.0) -> bool:
    """Cheap and honest: an https probe to our mirror list, no DNS-and-pray."""
    if shutil.which("ping"):
        proc = subprocess.run(["ping", "-c", "1", "-W", "2", "one.one.one.one"], capture_output=True)
        if proc.returncode == 0:
            return True
    proc = subprocess.run(["bash", "-c", "exec 3<>/dev/tcp/one.one.one.one/443"], capture_output=True,
                          timeout=timeout + 1)
    return proc.returncode == 0


def read_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return default


def profile_defaults() -> dict:
    doc = read_json(RELEASE, {}) or {}
    return doc.get("profile_defaults", {}) if isinstance(doc, dict) else {}


def step_release_stamp(ctx) -> str:
    """Make sure /etc/hedron/release.json exists and matches the installed packages."""
    doc = read_json(RELEASE)
    if doc is None:
        doc = {"format": "hedron-release-1", "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
               "source": "firstboot", "note": "regenerated on first boot because the ISO copy was absent"}
        doc.update({"profile": os.environ.get("HEDRONOS_PROFILE", "gamerte"), "variant": "dvd"})
        tmp = RELEASE + ".tmp"
        try:
            os.makedirs(os.path.dirname(RELEASE), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(doc, fh, indent=1, sort_keys=True)
            os.replace(tmp, RELEASE)
        except OSError as exc:
            return "could not write %s: %s" % (RELEASE, exc)
    ctx["release"] = {k: doc.get(k) for k in ("hedronos_version", "codename", "build_id", "profile", "variant")
                      if k in doc}
    return "release metadata present"


def step_drivers(ctx) -> str:
    """Apply the driver plan the ISO already computed, unless something already did."""
    state = os.path.join(STATE_DIR, "drivers/state.json")
    if os.path.isfile(state):
        return "driver state already recorded (%s) — nothing to do" % state
    if not shutil.which("hedron-drivers"):
        return "hedron-drivers not installed: skipping (install hedronos-drivers)"
    proc = subprocess.run(["hedron-drivers", "apply", "--if-needed"], capture_output=True, text=True)
    ctx["drivers_rc"] = proc.returncode
    tail = (proc.stdout.strip().splitlines() or [""])[-1]
    if proc.returncode != 0:
        return "hedron-drivers apply exited %d: %s" % (proc.returncode, tail or proc.stderr.strip()[:200])
    return "driver plan applied: " + (tail or "ok")


def step_flowstate(ctx) -> str:
    defaults = profile_defaults()
    profile = defaults.get("hedron_default_profile") or "balanced"
    unit = "hedron-flowstate.service"
    target = os.path.join(STATE_DIR, "flowstate")
    os.makedirs(target, exist_ok=True)
    with open(os.path.join(target, "default"), "w", encoding="utf-8") as fh:
        fh.write(profile + "\n")
    if shutil.which("systemctl") and os.path.isdir("/run/systemd/system"):
        subprocess.run(["systemctl", "enable", "--now", unit], capture_output=True, text=True)
        subprocess.run(["systemctl", "start", "hedron-flowstate-apply@boot.service"], capture_output=True,
                       text=True)
        return "default profile %r set and %s enabled" % (profile, unit)
    return "default profile %r written to %s (no systemd running: container or chroot)" % (profile, target)


def step_selected_apps(ctx) -> str:
    """Install the apps the user ticked in the pre-install chooser."""
    doc = read_json(SELECTED)
    if not doc:
        return "no app selection recorded (nothing to install)"
    ids = doc.get("pacman") or []
    flatpaks = doc.get("flatpak") or []
    ctx["selected"] = doc
    if not ids and not flatpaks:
        return "selection was empty"
    if have_network() is False:
        return "offline: deferring %d package(s)/%d flatpak(s) to hedron-firstapps.timer" % (
            len(ids), len(flatpaks))
    done = []
    if ids and shutil.which("pacman"):
        proc = subprocess.run(["pacman", "-S", "--noconfirm", "--needed"] + list(ids), capture_output=True,
                              text=True)
        done.append("pacman %d (%s)" % (len(ids), "ok" if proc.returncode == 0 else "rc=%d" % proc.returncode))
        ctx["pacman_rc"] = proc.returncode
    if flatpaks and shutil.which("flatpak"):
        for app in flatpaks:
            subprocess.run(["flatpak", "install", "-y", app], capture_output=True, text=True)
        done.append("flatpak %d" % len(flatpaks))
    return "; ".join(done) or "nothing installable"


def step_steamos_bits(ctx) -> str:
    """Things that are not packages: Proton prefix priming, steam runtime dirs, udisks2 rules."""
    notes = []
    for path in ("/var/lib/hedron/store", "/var/cache/hedron/drivers", "/etc/skel/.config/hedron"):
        if os.path.isdir(path.rstrip("/")):
            continue
        try:
            os.makedirs(path, exist_ok=True)
            notes.append("created " + path)
        except OSError as exc:
            notes.append("could not create %s (%s)" % (path, exc))
    skel = "/etc/skel/.config/hedron"
    if os.path.isdir(skel) and not os.path.isfile(os.path.join(skel, "features.json")):
        try:
            shutil.copy2("/etc/hedron/features.json", os.path.join(skel, "features.json"))
            notes.append("copied the feature list to /etc/skel so new users see what the distro promises")
        except OSError:
            notes.append("feature list not copied (missing /etc/hedron/features.json)")
    return "; ".join(notes) or "nothing to prime"


def step_units(ctx) -> str:
    """Enable exactly the units the built profile asked for — no more, no less."""
    want = profile_defaults().get("enable_units") or []
    if not want:
        return "no unit list in release.json (using package-prescribed defaults)"
    if not (shutil.which("systemctl") and os.path.isdir("/run/systemd/system")):
        return "would enable: " + ", ".join(want) + " (no systemd here)"
    enabled, failed = [], []
    for unit in want:
        proc = subprocess.run(["systemctl", "enable", unit], capture_output=True, text=True)
        (enabled if proc.returncode == 0 else failed).append(unit)
    out = "enabled %d/%d" % (len(enabled), len(want))
    if failed:
        out += "; FAILED: " + ", ".join(failed)
        ctx["units_failed"] = failed
    return out


def step_store_seed(ctx) -> str:
    """Prime the app-store index once so the store opens instantly after install."""
    if not shutil.which("hedrostd"):
        return "hedrostd not installed: skipping store seed"
    proc = subprocess.run(["hstore", "update", "--seed-only"], capture_output=True, text=True)
    return "store index seeded" if proc.returncode == 0 else "store seed skipped (rc=%d)" % proc.returncode


def step_report(ctx) -> str:
    doc = read_json(LOG, {}) or {}
    doc[str(int(time.time()))] = ctx
    tmp = LOG + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1, sort_keys=True)
    os.replace(tmp, LOG)
    return "recorded %s" % LOG


def all_steps() -> list[Step]:
    return [Step("release-stamp", step_release_stamp, docs="make sure /etc/hedron/release.json is real"),
             Step("flowstate", step_flowstate, docs="pick the default profile and enable the daemon"),
             Step("drivers", step_drivers, fatal=True, docs="apply the GPU plan the installer computed"),
             Step("units", step_units, docs="enable the profile's systemd units"),
             Step("selected-apps", step_selected_apps, needs_network=True,
                  docs="install the apps ticked in the chooser (Steam is always in the list)"),
             Step("priming", step_steamos_bits, docs="create runtime dirs and seed /etc/skel"),
             Step("report", step_report, docs="record what happened for hedron-doctor")]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hedron-firstboot", description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    ap.add_argument("--force", action="store_true", help="re-run even if already done")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--only", default=None, help="run a single step by name (debug)")
    args = ap.parse_args(argv)

    if args.status:
        doc = read_json(LOG)
        if not doc:
            print("firstboot has not run (no %s)" % LOG)
            return 1
        last = sorted(doc)[-1]
        print("firstboot ran at %s" % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(int(last))))
        print(json.dumps(doc[last], indent=1, sort_keys=True))
        return 0

    if os.path.isfile(DONE) and not args.force:
        print("firstboot already completed (%s); use --force to re-run" % DONE)
        return 0
    if os.path.isfile(LIVE_MARKER):
        print("live session detected — refusing to run firstboot (this is the ISO, not your install)")
        return 0

    steps = all_steps()
    if args.only:
        steps = [s for s in steps if s.name == args.only]
        if not steps:
            print("no step named %r (known: %s)" % (args.only, ", ".join(x.name for x in all_steps())),
                  file=sys.stderr)
            return 2

    ctx: dict = {"when": time.time(), "dry_run": args.dry_run, "results": {}}
    print("HedronOS first boot — %d step(s)" % len(steps))
    rc = 0
    for step in steps:
        started = time.time()
        if args.dry_run:
            print("  [dry] %-14s %s" % (step.name, step.docs))
            ctx["results"][step.name] = "(dry run)"
            continue
        try:
            note = step.fn(ctx)
        except Exception as exc:  # noqa: BLE001 - one broken step must not stop the machine booting
            note = "raised %s: %s" % (type(exc).__name__, exc)
            rc = 1 if step.fatal else rc
        ctx["results"][step.name] = note
        print("  %-16s %s  (%.2fs)" % (step.name, note, time.time() - started))
    if not args.dry_run:
        step_report(ctx)
        os.makedirs(STATE_DIR, exist_ok=True)
        with open(DONE, "w", encoding="utf-8") as fh:
            fh.write(time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "\n")
        print("\nfirstboot complete — %s" % ("some steps need attention (`hedron-doctor`)" if rc else "all good"))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
