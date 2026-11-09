#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""hedronflowd — FlowState's activation daemon.

Watches for three things and flips the transactional profile accordingly:

  1. a foreground app that deserves `gaming` (via the session bridge: KWin/KWin-wayland
     posts `focus-changed` to a D-Bus-free UNIX socket, gamescope posts `layer:fullscreen`)
  2. a game launching through `hedron-run`/GameMode (the strongest signal we get)
  3. power state (unplug on a laptop → `balanced` or `battery`, unless a game owns the
     screen), plus thermal pressure → `silence`

It never edits your settings; it only *selects a profile*, and every profile change is a
`hedron-tune apply --transactional` call that can be reverted. When this daemon dies, the
last profile simply stays — which is why the units are `Restart=always` but the state file
is authoritative.
"""

from __future__ import annotations

import argparse
import json
import os
import selectors
import signal
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

from hedronlib.util import load_toml  # noqa: E402

SOCK = "/run/hedron/flowstate.sock"
STATE = "/var/lib/hedron/flowstate/state.json"
CONF = "/etc/hedron/flowstate.conf"
OVERRIDE = "/etc/hedron/flowstate.local.toml"
GAME_HINTS = ("steam.exe", "proton", "wine64", "gamescope", "heroic", "lutris", "mangohud",
              "minecraft", "fortnite", "eac", "batl", "nvidia-egl-wayland")


def conf() -> dict:
    out = {"enabled": True, "auto_gaming": True, "auto_power": True, "auto_thermal": True,
           "engage_on": ["gamemode", "fullscreen", "hedron-run"],
           "profile_gaming": "gaming", "profile_desktop": "balanced",
           "profile_battery": "battery", "profile_thermal": "silence",
           "release_delay_s": 12, "thermal_temp_c": 88, "poll_s": 2.0,
           "hold_while_recording": True}
    if os.path.isfile(CONF):
        for line in open(CONF, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip('"')
            if key == "engage_on":
                out[key] = [v.strip() for v in value.split(",") if v.strip()]
            elif value.lower() in ("true", "false"):
                out[key] = value.lower() == "true"
            elif value.replace(".", "", 1).isdigit():
                out[key] = float(value) if "." in value else int(value)
            else:
                out[key] = value
    if os.path.isfile(OVERRIDE):
        try:
            out.update(load_toml(OVERRIDE).get("flow", {}))
        except Exception:  # noqa: BLE001
            pass
    return out


def active() -> str:
    try:
        with open("/var/lib/hedron/flowstate/active", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def apply_profile(name: str, reason: str, cfg: dict) -> bool:
    if not name or name == active():
        return False
    started = time.time()
    try:
        proc = subprocess.run(["hedron-tune", "apply", name, "--reason", reason[:120]],
                              capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError) as exc:
        log("failed to invoke hedron-tune: %s" % exc)
        return False
    ok = proc.returncode == 0
    log("apply %s (%s) → %s in %.2fs" % (name, reason, "ok" if ok else "FAILED", time.time() - started))
    if not ok:
        log("  stderr: " + (proc.stderr.strip().splitlines() or [""])[0][:200])
    return ok


LOGGED = []


def log(msg: str) -> None:
    line = "%s hedronflowd: %s" % (time.strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    LOGGED.append(line)
    if len(LOGGED) > 500:
        del LOGGED[:250]


# ------------------------------------------------------------------ input signals
def fullscreen_apps() -> list[str]:
    """Ask the session bridge which window owns the screen (no-op outside a session)."""
    for helper in ("hedron-session-query", "kdotool", "wlrctl", "xdotool"):
        if not shutil_which(helper):
            continue
        try:
            if helper == "hedron-session-query":
                out = subprocess.run([helper, "--json", "focused"], capture_output=True, text=True, timeout=5)
                if out.returncode == 0:
                    data = json.loads(out.stdout or "{}")
                    return [data.get("app_id", ""), data.get("state", "")]
            if helper == "xdotool":
                out = subprocess.run([helper, "getactivewindow", "getwindowname"], capture_output=True,
                                     text=True, timeout=5)
                return [out.stdout.strip(), "unknown"]
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
    return []


def shutil_which(name: str):
    from shutil import which
    return which(name)


def gamemode_active() -> dict:
    """GameMode's daemon already knows the answer; ask it instead of guessing."""
    if not os.path.exists("/run/hedron/gamemode.sock") and not shutil_which("hgm"):
        return {}
    try:
        out = subprocess.run(["hgm", "status", "--json"], capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            return json.loads(out.stdout or "{}")
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    return {}


def thermal_state(cfg: dict) -> bool:
    """True when we should back off (any package over thermal throttle temperature)."""
    limit = float(cfg.get("thermal_temp_c", 88))
    for path in sorted(sum([__import__("glob").glob(p, recursive=True) for p in (
            "/sys/class/hwmon/hwmon*/temp*_input", "/sys/class/thermal/thermal_zone*/temp")], [])):
        try:
            with open(path, encoding="utf-8") as fh:
                raw = fh.read().strip()
            value = int(raw)
            value = value / 1000.0 if value > 1000 else float(value)
        except (OSError, ValueError):
            continue
        if value >= limit:
            return True
    return False


def on_battery() -> bool:
    for path in __import__("glob").glob("/sys/class/power_supply/BAT*/status"):
        try:
            with open(path, encoding="utf-8") as fh:
                if fh.read().strip().lower() == "discharging":
                    return True
        except OSError:
            continue
    return False


def recording() -> bool:
    return os.path.exists("/run/hedron/replay.lock")


# ----------------------------------------------------------------------- main loop
def daemonize_socket(cfg: dict) -> socket.socket | None:
    try:
        os.makedirs(os.path.dirname(SOCK), exist_ok=True)
        if os.path.exists(SOCK):
            os.unlink(SOCK)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(SOCK)
        sock.listen(16)
        sock.setblocking(False)
        os.chmod(SOCK, 0o660)
        return sock
    except OSError as exc:
        log("cannot bind %s (%s); continuing without the session bridge" % (SOCK, exc))
        return None


def handle_line(line: str, cfg: dict) -> str:
    """Process one bridge message; returns a human summary of the decision."""
    parts = line.strip().split(None, 2)
    if not parts:
        return "empty"
    kind = parts[0]
    payload = parts[1] if len(parts) > 1 else ""
    detail = parts[2] if len(parts) > 2 else ""
    if kind == "focus-changed":
        lowered = (payload + " " + detail).lower()
        looks_game = any(h in lowered for h in GAME_HINTS)
        if "fullscreen" in lowered or looks_game:
            if apply_profile(cfg["profile_gaming"], "focus:%s" % payload, cfg):
                return "engaged gaming for %s" % payload
        return "focus %s (%s) — no change" % (payload, detail or "-")
    if kind == "gamemode-enter":
        apply_profile(cfg["profile_gaming"], "gamemode:%s" % payload, cfg)
        return "engaged gaming for pid %s" % payload
    if kind == "gamemode-leave":
        return "gamemode released; reverting after delay"
    if kind == "layer":
        if payload == "fullscreen":
            apply_profile(cfg["profile_gaming"], "layer-fullscreen", cfg)
            return "fullscreen layer → gaming"
        return "layer back to desktop"
    if kind == "profile":
        apply_profile(payload or cfg["profile_desktop"], "manual", cfg)
        return "profile set to %s" % payload
    if kind == "ping":
        return "pong %s" % (active() or "none")
    return "ignored %r" % kind


def loop(cfg: dict, one_shot: bool = False) -> int:
    sel = selectors.DefaultSelector()
    sock = daemonize_socket(cfg)
    if sock:
        sel.register(sock, selectors.EVENT_READ, "bridge")
    last_release = 0.0
    engaged = False
    stop = {"flag": False}

    def on_term(_signum, _frame):
        stop["flag"] = True
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)

    log("flowstate daemon up (auto_gaming=%s auto_power=%s auto_thermal=%s, poll=%ss)"
        % (cfg["auto_gaming"], cfg["auto_power"], cfg["auto_thermal"], cfg["poll_s"]))
    while not stop["flag"]:
        events = sel.select(timeout=cfg["poll_s"]) if sock else []
        for key, _ in events:
            try:
                conn, _ = key.fileobj.accept()
            except OSError:
                continue
            with conn:
                conn.settimeout(5)
                try:
                    data = conn.recv(4096)
                except OSError:
                    data = b""
                if data:
                    for line in data.decode("utf-8", "replace").splitlines():
                        if not line.strip():
                            continue
                        verdict = handle_line(line, cfg)
                        if verdict.startswith("engaged") or verdict.startswith("profile") or verdict.startswith("fullscreen"):
                            engaged = True
                        try:
                            conn.sendall((json.dumps({"ok": True, "decision": verdict,
                                                       "active": active()}) + "\n").encode())
                        except OSError:
                            pass
        # -- autonomous rules (the stuff that makes it feel automatic)
        if cfg["auto_gaming"]:
            gm = gamemode_active()
            if gm.get("clients"):
                if apply_profile(cfg["profile_gaming"], "gamemode-active", cfg):
                    engaged = True
                last_release = 0.0
            elif engaged and not gm.get("clients"):
                now = time.time()
                if not last_release:
                    last_release = now
                elif now - last_release > float(cfg["release_delay_s"]):
                    target = cfg["profile_battery"] if (cfg["auto_power"] and on_battery()) \
                        else cfg["profile_desktop"]
                    apply_profile(target, "release", cfg)
                    engaged, last_release = False, 0.0
        if not engaged and cfg["auto_thermal"] and thermal_state(cfg):
            if apply_profile(cfg["profile_thermal"], "thermal", cfg):
                log("thermal pressure: %s engaged" % cfg["profile_thermal"])
        if not engaged and cfg["auto_power"] and on_battery() and active() != cfg["profile_battery"]:
            apply_profile(cfg["profile_battery"], "on-battery", cfg)
        if recording() and active() != cfg["profile_gaming"] and engaged:
            apply_profile(cfg["profile_gaming"], "recording", cfg)
        write_state(engaged, cfg)
        if one_shot:
            break
    if sock:
        sel.unregister(sock)
        sock.close()
        try:
            os.unlink(SOCK)
        except OSError:
            pass
    log("shutting down (state preserved)")
    return 0


def write_state(engaged: bool, cfg: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        tmp = STATE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"when": int(time.time()), "engaged": engaged, "active": active(),
                       "profiles": {"gaming": cfg["profile_gaming"], "desktop": cfg["profile_desktop"]}},
                      fh, sort_keys=True)
        os.replace(tmp, STATE)
    except OSError:
        pass


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hedronflowd", description=__doc__.splitlines()[0])
    ap.add_argument("--oneshot", action="store_true", help="evaluate once and exit (cron/timer)")
    ap.add_argument("--once", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--explain", action="store_true", help="print the decision, do not apply")
    ap.add_argument("--line", default=None, help="feed one bridge line (used by tests and `--dry-run`)")
    args = ap.parse_args(argv)
    cfg = conf()
    if args.line is not None:
        print(handle_line(args.line, cfg) if not args.explain else "(explain) " + args.line)
        return 0
    if args.explain:
        print(json.dumps({"config": cfg, "active": active(), "battery": on_battery(),
                          "thermal": thermal_state(cfg), "gamemode": gamemode_active(),
                          "focused": fullscreen_apps()}, indent=1, sort_keys=True))
        return 0
    return loop(cfg, one_shot=args.oneshot or args.once)


if __name__ == "__main__":
    raise SystemExit(main())
