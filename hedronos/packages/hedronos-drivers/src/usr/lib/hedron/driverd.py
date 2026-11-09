#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""hedronos driver convergence daemon.

Three modes, one engine:

  --once --converge   boot/service mode: probe, plan, compare against what is *actually*
                      true (modules loaded, files written, packages installed) and apply
                      only the delta.  Safe to run on every boot.
  --serve             socket-activated JSON-lines API used by the installer, the store
                      and `hedron-doctor`.  Read-only.
  --hotplug           invoked by a udev rule when a display device appears/disappears
                      (external GPU dock, eGPU box, Thunderbolt).

Why converge instead of "install once"? Because the two things that break graphics on
Linux happen *after* the install: a kernel update invalidates a DKMS build, and someone
installs a package that blacklists the vendor module. A distro that notices is a
distro you can trust with a game library.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time

# repo checkout (packages/*/src/usr/bin) without an install step, so try both locations.
_HERE = os.path.dirname(os.path.abspath(__file__))
# Path bootstrap: the same file must work installed (/usr/lib/…) and straight from a repo
# checkout (packages/<pkg>/src/usr/…) with no install step, so walk up looking for the
# vendored library instead of hard-coding a depth that changes with the packaging layout.
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

from hedronlib import drivers, gpus  # noqa: E402
from hedronlib.util import load_json, load_toml  # noqa: E402

CONF = "/etc/hedron/drivers.conf"
DB = "/etc/hedron/gpu-db.json"
RULES = "/etc/hedron/driver-rules.toml"
PLANS = "/etc/hedron/gpu-plans.json"
STATE = "/var/lib/hedron/drivers"
STAMP = os.path.join(STATE, "converged.json")
LOG = "/var/log/hedron-drivers.log"


def log(msg: str) -> None:
    line = "%s %s" % (time.strftime("%Y-%m-%dT%H:%M:%S%z"), msg)
    print(line, flush=True)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def conf_value(key: str, default):
    conf = {}
    if os.path.isfile(CONF):
        for line in open(CONF, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            conf[k.strip()] = v.strip().strip('"')
    value = conf.get(key)
    if value is None:
        return default
    if isinstance(default, bool):
        return value.lower() in ("1", "true", "yes", "on")
    if isinstance(default, int):
        try:
            return int(value)
        except ValueError:
            return default
    return value


# --------------------------------------------------------------------- state
def current_state(info: gpus.SystemInfo, plan: drivers.DriverPlan) -> dict:
    """What is true right now, so we can compare it with what the plan wants."""
    state = {"modules_loaded": sorted(info.modules_loaded), "files": {}, "cmdline": "",
             "kernel": info.kernel, "packages": [], "db_mtime": None}
    try:
        state["cmdline"] = open("/proc/cmdline", encoding="utf-8").read().strip()
    except OSError:
        pass
    for name in plan.modprobe_files:
        path = "/etc/modprobe.d/" + name
        state["files"][path] = os.path.isfile(path)
    for path in plan.files:
        state["files"][path] = os.path.isfile(path)
    if os.path.exists(DB):
        state["db_mtime"] = int(os.path.getmtime(DB))
    # Ask pacman which of our packages are actually installed (cheap, offline).
    wanted = plan.package_names(["required"])
    if wanted and shutil_which("pacman"):
        proc = subprocess.run(["pacman", "-Q", "--quiet"] + wanted, capture_output=True, text=True)
        installed = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
        state["packages"] = sorted(installed)
        state["missing_packages"] = sorted(set(wanted) - installed)
    return state


def shutil_which(name: str):
    from shutil import which
    return which(name)


def needs_attention(info: gpus.SystemInfo, plan: drivers.DriverPlan, state: dict) -> list[str]:
    reasons: list[str] = []
    if plan.kmod and plan.kmod not in state["modules_loaded"]:
        # Bound driver may differ from kmod when the vendor module takes over
        bound = [d.driver for d in info.devices if d.is_gpu and d.driver]
        if plan.kmod not in bound and bound:
            pass
        elif bound:
            reasons.append("device is bound to %s but the plan wants %s" % (", ".join(sorted(set(bound))), plan.kmod))
        else:
            reasons.append("no driver is bound to the display device (plan wants %s)" % plan.kmod)
    for path, present in state["files"].items():
        if not present:
            reasons.append("configuration file missing: %s" % path)
    for opt in plan.cmdline:
        if opt.split("=")[0] not in state["cmdline"] and "=" in opt:
            reasons.append("kernel parameter not active: %s" % opt)
    if state.get("missing_packages"):
        reasons.append("packages not installed: " + " ".join(state["missing_packages"][:6]))
    if plan.vendor == "nvidia" and plan.branch.startswith("open"):
        if not any(name.startswith(("nvidia",)) for name in state["modules_loaded"]):
            reasons.append("NVIDIA module not loaded — likely a kernel update invalidated DKMS")
    return reasons


# ----------------------------------------------------------------- apply delta
def converge(dry: bool = False, force: bool = False) -> int:
    if not os.path.exists(DB) or not os.path.exists(RULES):
        log("no database/rules on disk yet (live ISO before install, or broken package); nothing to do")
        return 0
    db = gpus.GpuDatabase(load_json(DB))
    rules = load_toml(RULES)
    local = "/etc/hedron/drivers.local.toml"
    if os.path.isfile(local):
        try:
            rules = load_toml(local).get("override", {}).get("rules", rules) or rules
        except Exception as exc:  # noqa: BLE001
            log("ignoring broken %s: %s" % (local, exc))
    engine = drivers.DriverEngine(db, rules)
    info = gpus.collect_system_info()
    plan = engine.merged_plan(info)
    state = current_state(info, plan)
    reasons = needs_attention(info, plan, state)

    if not reasons and not force:
        log("converged already: %s/%s via %s — nothing to do" % (plan.vendor, plan.family, plan.branch))
        write_stamp(plan, reasons, applied=False)
        return 0
    log("needs attention: " + "; ".join(reasons))
    actions = drivers.install_actions(plan, dry_run=dry)
    if dry:
        for action in actions:
            log("  would %s: %s" % (action["kind"], action.get("path") or action.get("packages")
                                    or action.get("unit") or action.get("argv") or ""))
        return 0
    applied = run_actions(actions, plan)
    write_stamp(plan, reasons, applied=True)
    log("convergence %s (applied %d action(s))" % ("complete" if applied else "partial", len(actions)))
    return 0 if applied else 1


def run_actions(actions: list[dict], plan: drivers.DriverPlan) -> bool:
    """Execute only file/service deltas; package installs go through hedron-drivers --apply."""
    ok = True
    for action in actions:
        kind = action["kind"]
        try:
            if kind == "write-file":
                path = action["path"]
                os.makedirs(os.path.dirname(path) or "/", exist_ok=True)
                existing = ""
                if os.path.isfile(path):
                    existing = open(path, encoding="utf-8", errors="replace").read()
                if existing != action["content"]:
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write(action["content"])
                    log("  wrote %s" % path)
            elif kind == "set-env":
                path = action["path"]
                os.makedirs(os.path.dirname(path) or "/", exist_ok=True)
                values = action.get("values", {})
                body = "".join("%s=%s\n" % (k, " ".join(map(str, v)) if isinstance(v, list) else v)
                               for k, v in sorted(values.items()))
                if not (os.path.isfile(path) and open(path, encoding="utf-8").read() == body):
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write("# hedronos-driverd (generated)\n" + body)
                    log("  wrote %s" % path)
                if path.endswith("hedron-drivers.conf"):
                    subprocess.run(["mkinitcpio", "-p", "linux-hedron"], check=False,
                                   capture_output=True, text=True)
                    log("  rebuilt initramfs for module list change")
            elif kind == "enable-service":
                state = action.get("state", "enabled")
                unit = action["unit"] if "." in action["unit"] else action["unit"] + ".service"
                subprocess.run(["systemctl", state, unit], check=False, capture_output=True, text=True)
                log("  %s %s" % (state, unit))
            elif kind == "install-packages":
                missing = [p for p in action.get("packages", []) if p in (plan.package_names(["required"]))]
                if missing:
                    log("  packages missing (%d): deferring to hedron-drivers --apply (needs network or the on-ISO cache)"
                        % len(missing))
            elif kind == "kernel-cmdline":
                path = "/etc/hedron/cmdline.d/90-drivers.conf"
                os.makedirs(os.path.dirname(path), exist_ok=True)
                want = " ".join(action.get("add", [])) + "\n"
                if not (os.path.isfile(path) and open(path, encoding="utf-8").read() == want):
                    with open(path, "w", encoding="utf-8") as fh:
                        fh.write(want)
                    subprocess.run(["hedron-boot-entries", "--regenerate"], check=False,
                                   capture_output=True, text=True)
                    log("  updated kernel command line + bootloader entries")
            elif kind == "verify":
                checks = action.get("checks", [])
                log("  post-apply checks: %d" % len(checks))
        except OSError as exc:
            ok = False
            log("  action %s failed: %s" % (kind, exc))
    return ok


def write_stamp(plan: drivers.DriverPlan, reasons: list[str], applied: bool) -> None:
    os.makedirs(STATE, exist_ok=True)
    payload = {"when": int(time.time()), "reasons": reasons, "applied": applied,
               "plan": plan.as_dict(), "kernel": os.uname().release}
    tmp = STAMP + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, sort_keys=True)
    os.replace(tmp, STAMP)


def read_stamp() -> dict:
    try:
        return load_json(STAMP)
    except (OSError, ValueError):
        return {}


# ------------------------------------------------------------------ socket API
REQUEST_TIMEOUT = 60


def handle_request(req: dict) -> dict:
    cmd = req.get("cmd", "plan")
    try:
        db = gpus.GpuDatabase(load_json(DB))
        rules = load_toml(RULES)
    except (OSError, ValueError) as exc:
        return {"ok": False, "error": "engine unavailable: %s" % exc}
    engine = drivers.DriverEngine(db, rules)
    if cmd in ("ping", "status"):
        return {"ok": True, "service": "hedronos-driverd", "db": os.path.getmtime(DB) if os.path.exists(DB) else 0,
                "plans_cached": bool(os.path.exists(PLANS)), "stamp": read_stamp().get("when", 0)}
    if cmd == "explain":
        info = gpus.collect_system_info()
        plan = engine.merged_plan(info)
        return {"ok": True, "plan": plan.as_dict(), "probe": info.as_dict(),
                "state": current_state(info, plan), "reasons": needs_attention(info, plan, current_state(info, plan))}
    if cmd == "plan-for":
        # The installer asks: "what would you do for this capture?" (no root needed)
        devices = []
        for row in req.get("devices", []):
            devices.append(gpus.PciDevice(slot=row.get("slot", ""), vendor=gpus.norm_id(row["vendor"]),
                                           device=gpus.norm_id(row["device"]),
                                           class_code=gpus.norm_id(row.get("class", "0x030000")),
                                           driver=row.get("driver")))
        info = gpus.SystemInfo(devices=devices, kernel=req.get("kernel", ""),
                               secure_boot=bool(req.get("secure_boot", False)),
                               efi=bool(req.get("efi", True)),
                               dmi={"product_name": req.get("product", "")})
        plan = engine.merged_plan(info)
        return {"ok": True, "plan": plan.as_dict(), "actions": drivers.install_actions(plan, dry_run=True)}
    if cmd == "list-families":
        return {"ok": True, "families": db.families(req.get("vendor"))}
    return {"ok": False, "error": "unknown cmd %r (try: ping status explain plan-for list-families)" % cmd}


def serve() -> int:
    """systemd-socket-activate style: accept one connection per activation, then exit
    on idle — a daemon that cannot leak memory while nobody is asking."""
    fd_env = os.environ.get("LISTEN_FDS")
    if not fd_env:
        log("--serve needs socket activation (LISTEN_FDS); use `systemctl start hedronos-driverd.socket` "
            "or run `systemd-socket-activate -l /run/hedron/driverd.sock hedron-driverd`")
        return 2
    try:
        first = int(os.environ.get("LISTEN_PID", os.getpid()))
    except ValueError:
        first = os.getpid()
    if first != os.getpid():
        log("LISTEN_PID mismatch, ignoring inherited fds")
        return 2
    sock = socket.fromfd(3, socket.AF_UNIX, socket.SOCK_STREAM)
    sock.setblocking(False)
    idle_deadline = time.monotonic() + conf_value("idle_timeout_s", 60)
    log("driverd serving on /run/hedron/driverd.sock")
    while time.monotonic() < idle_deadline:
        try:
            conn, _ = sock.accept()
        except (BlockingIOError, TimeoutError):
            time.sleep(0.5)
            continue
        except OSError as exc:
            log("accept failed: %s" % exc)
            return 1
        idle_deadline = time.monotonic() + conf_value("idle_timeout_s", 60)
        with conn:
            conn.settimeout(REQUEST_TIMEOUT)
            try:
                for line in conn.makefile("r"):
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        req = json.loads(line)
                    except ValueError as exc:
                        conn.sendall((json.dumps({"ok": False, "error": "bad json: %s" % exc}) + "\n").encode())
                        continue
                    reply = handle_request(req)
                    conn.sendall((json.dumps(reply, sort_keys=True) + "\n").encode())
            except OSError as exc:
                log("connection error: %s" % exc)
    log("idle timeout reached, exiting (socket will reactivate on demand)")
    return 0


def hotplug() -> int:
    """Called from a udev rule. Only log + converge if the plan actually changed."""
    stamp = read_stamp()
    if not os.path.exists(DB):
        return 0
    db = gpus.GpuDatabase(load_json(DB))
    engine = drivers.DriverEngine(db, load_toml(RULES))
    info = gpus.collect_system_info()
    plan = engine.merged_plan(info)
    previous = (stamp.get("plan") or {}).get("branch", "")
    if previous and previous != plan.branch:
        log("hotplug changed the plan: %s → %s" % (previous, plan.branch))
        return converge(dry=False, force=True)
    log("hotplug: plan unchanged (%s)" % plan.branch)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="driverd.py", description=__doc__.splitlines()[0])
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--once", action="store_true", help="converge once and exit")
    mode.add_argument("--serve", action="store_true", help="serve the JSON API on the activated socket")
    mode.add_argument("--hotplug", action="store_true", help="udev-triggered re-plan")
    ap.add_argument("--converge", action="store_true", help="with --once: apply deltas, not just report")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--status", action="store_true", help="print the last convergence stamp and exit")
    args = ap.parse_args(argv)

    if args.status:
        stamp = read_stamp()
        if not stamp:
            print("no convergence record yet (%s missing)" % STAMP)
            return 0
        print("last run : %s" % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp.get("when", 0))))
        print("kernel   : %s (now %s)" % (stamp.get("kernel"), os.uname().release))
        print("applied  : %s" % stamp.get("applied"))
        print("plan     : %s/%s branch=%s" % ((stamp.get("plan") or {}).get("vendor"),
                                             (stamp.get("plan") or {}).get("family"),
                                             (stamp.get("plan") or {}).get("branch")))
        for reason in stamp.get("reasons", []):
            print("  reason : " + reason)
        stale = stamp.get("kernel") != os.uname().release
        if stale:
            print("note: the kernel changed since the last run — DKMS modules may need a rebuild "
                  "(`sudo hedron-drivers --apply`)")
        return 0
    if args.serve:
        return serve()
    if args.hotplug:
        return hotplug()
    if args.once and not args.converge:
        return converge(dry=not args.force, force=args.force)
    return converge(dry=args.dry_run, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
