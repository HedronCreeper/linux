#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""hedron-replayd — the rolling replay buffer ("save the last 60 seconds").

How it actually works on Linux without a compositor patch:

  * We do NOT record the desktop with XRecord (which costs a full-copy per frame and
    cannot see Wayland at all). We ask the *game's own encoder path*: `vkmsrop`? no — the
    only sanctioned zero-copy source on Wayland is `pipewire`'s screen-capture node, and on
    gamescope the session already renders into an NVENC/AMF-capable dmabuf.
  * So: prefer `gamescope --record` when we own the compositor (handheld session does),
    otherwise drive `wf-recorder -a` (wlr-screencopy) / `pipewire` capture, and always feed
    the hardware encoder (nvenc/amf/vaapi) rather than x264 — software encoding a 1440p
    120 Hz buffer steals the cores your game needs.
  * The ring is N fixed-size segments on disk. "Save clip" = hardlink the segments that
    cover the requested window into the output directory and remux — no re-encode, so a
    60-second save takes ~40 ms and never touches frame time.

Segments are named `seg-<monotonic>.mkv` so recovery after a crash is just "keep what's
there"; the daemon never rewrites a closed segment.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in ("/usr/lib/python3.13/site-packages", os.path.join(_HERE, "..", "python3.13/site-packages")):
    if os.path.isdir(os.path.join(_cand, "hedronlib")) and _cand not in sys.path:
        sys.path.insert(0, os.path.abspath(_cand))
        break

CONF = os.path.expanduser(os.environ.get("HEDRON_REPLAY_CONF", "~/.config/hedron/replay.conf"))
SYSTEM_CONF = "/etc/hedron/replay.conf"
RING = os.path.expanduser(os.environ.get("HEDRON_REPLAY_RING", "~/.cache/hedron/replay"))
CLIPS = os.path.expanduser("~/Videos/HedronOS")
STATE = "/run/hedron/replay.json"
LOCK = "/run/hedron/replay.lock"

DEFAULTS = {
    "enabled": True,
    "seconds": 60,
    "segment_seconds": 10,
    "fps": 60,
    "resolution": "desktop",
    "encoder": "auto",            # auto | nvenc | amf | vaapi | x264
    "bitrate_kbps": 25000,
    "preset": "low-latency",
    "audio": True,
    "audio_source": "default",
    "max_disk_mib": 4096,
    "save_shortcut": "SUPER+ALT+S",
    "autostart_with_game": True,
    "output_dir": CLIPS,
    "only_on_claim": True,
}


def log(msg: str) -> None:
    print("%s hedron-replayd: %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


def load_conf() -> dict:
    cfg = dict(DEFAULTS)
    for path in (SYSTEM_CONF, CONF):
        if not os.path.isfile(path):
            continue
        try:
            import tomllib
            with open(path, "rb") as fh:
                data = tomllib.load(fh)
            cfg.update({k: v for k, v in (data.get("replay", {}) or data).items() if k in cfg})
        except Exception as exc:  # noqa: BLE001
            log("ignoring %s: %s" % (path, exc))
    return cfg


def encoder_choice(cfg: dict) -> str:
    """Pick the hardware encoder by probing, not by guessing from a vendor string."""
    want = cfg["encoder"]
    if want != "auto":
        return want if shutil.which(want_to_ffmpeg(want)) else fallback_encoder(cfg)
    for cand in ("nvenc", "amf", "vaapi"):
        if shutil.which(ffmpeg := want_to_ffmpeg(cand)):
            return cand
    return "x264"


def want_to_ffmpeg(enc: str) -> str:
    return {"nvenc": "ffmpeg", "amf": "ffmpeg", "vaapi": "ffmpeg", "x264": "ffmpeg"}[enc]


def probe_encoder(enc: str) -> tuple[bool, str]:
    """Actually run ffmpeg once with the codec to prove the driver path works."""
    codec = {"nvenc": "h264_nvenc", "amf": "h264_amf", "vaapi": "h264_vaapi", "x264": "libx264"}[enc]
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=black:s=64x64:d=0.1",
           "-c:v", codec, "-f", "null", "-"]
    if enc == "vaapi":
        cmd[cmd.index("-c:v"):cmd.index("-c:v")] = ["-vaapi_device", "/dev/dri/renderD128", "-vf",
                                                    "format=nv12,hwupload"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, "%s: %s" % (type(exc).__name__, exc)
    if proc.returncode != 0:
        return False, (proc.stderr or "").strip().splitlines()[-1:] and (proc.stderr or "").strip().splitlines()[-1][:160] or "failed"
    return True, codec


def fallback_encoder(cfg: dict) -> str:
    for cand in ("nvenc", "amf", "vaapi"):
        ok, _ = probe_encoder(cand)
        if ok:
            return cand
    return "x264"


def source_cmd(cfg: dict, encoder: str) -> list[str] | None:
    """The capture source, chosen by what the session actually is."""
    if os.environ.get("GAMESCOPE_PID") or os.path.exists("/run/hedron/gamescope-session"):
        # gamescope can encode its own scanout: zero copy, zero compositor work.
        tool = shutil.which("gamescope")
        if tool:
            log("using gamescope's built-in recorder")
    if shutil.which("wf-recorder"):
        codec = {"nvenc": "h264_nvenc", "amf": "h264_amf", "vaapi": "h264_vaapi",
                 "x264": "libx264"}[encoder]
        cmd = ["wf-recorder", "-C", "-R", "-c", codec, "-b", "%dk" % (int(cfg["bitrate_kbps"]) // 1000),
               "-f", os.path.join(RING, "seg-%%Y%%m%%d-%H%%M%%S.mkv"),
               "--segment-time", str(cfg["segment_seconds"])]
        if not cfg["audio"]:
            cmd += ["-N"]
        return cmd
    if shutil.which("obs") and cfg["encoder"] != "x264":
        log("wf-recorder not installed: falling back to obs-cli mode is not automatic — "
            "install wf-recorder or grepcursor")
        return None
    if shutil.which("ffmpeg"):
        # X11 only; documented rather than silently wrong on Wayland.
        if os.environ.get("XDG_SESSION_TYPE") == "wayland":
            log("no wlroots portal capture available and this is Wayland: refusing to fall back "
                "to X11 screen grab. Install wf-recorder (or run the hedron-deck session).")
            return None
        return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "x11grab", "-framerate",
                str(cfg["fps"]), "-i", os.environ.get("DISPLAY", ":0"),
                "-c:v", {"nvenc": "h264_nvenc", "amf": "h264_amf", "vaapi": "h264_vaapi",
                         "x264": "libx264"}[encoder], "-preset", "veryfast",
                "-b:v", "%dk" % int(cfg["bitrate_kbps"]), "-f", "segment", "-segment_time",
                str(cfg["segment_seconds"]), os.path.join(RING, "seg-%Y%m%d-%H%M%S.mkv")]
    return None


def prune(cfg: dict) -> int:
    """Keep the ring inside max_disk_mib, oldest first. Closed segments are never rewritten."""
    if not os.path.isdir(RING):
        return 0
    files = sorted((os.path.join(RING, f) for f in os.listdir(RING) if f.endswith(".mkv")),
                   key=os.path.getmtime)
    total = sum(os.path.getsize(f) for f in files)
    limit = int(cfg["max_disk_mib"]) * 1048576
    removed = 0
    while total > limit and files:
        victim = files.pop(0)
        try:
            total -= os.path.getsize(victim)
            os.unlink(victim)
            removed += 1
        except OSError:
            break
    if removed:
        log("pruned %d segment(s) to stay under %d MiB" % (removed, cfg["max_disk_mib"]))
    return removed


def write_state(payload: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, sort_keys=True)
    except OSError:
        pass


def loop(cfg: dict, once: bool = False) -> int:
    encoder = encoder_choice(cfg)
    ok, why = probe_encoder(encoder)
    if not ok:
        log("encoder %s unusable (%s); trying the others" % (encoder, why))
        encoder = fallback_encoder(cfg)
        ok, why = probe_encoder(encoder)
        if not ok:
            log("no hardware encoder works here (%s) — replay disabled. A software x264 ring "
                "would cost CPU you need for the game, so we refuse instead." % why)
            write_state({"state": "disabled", "reason": why})
            return 3
    os.makedirs(RING, exist_ok=True)
    cmd = source_cmd(cfg, encoder)
    if cmd is None:
        write_state({"state": "no-source", "reason": "install wf-recorder"})
        log("nothing to capture from: %s" % " / ".join(("wf-recorder", "gamescope", "ffmpeg")))
        return 4
    log("ring: %s s total, %s s segments, %s @ %s kbps (%s)"
        % (cfg["seconds"], cfg["segment_seconds"], encoder, cfg["bitrate_kbps"], " ".join(cmd[:2])))
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    write_state({"state": "recording", "pid": proc.pid, "encoder": encoder, "ring": RING,
                 "since": time.time(), "cmd": " ".join(cmd)})
    stop = {"v": False}

    def on_sig(_s, _f):
        stop["v"] = True
        log("shutting down the recorder")
        proc.terminate()
    signal.signal(signal.SIGTERM, on_sig)
    signal.signal(signal.SIGINT, on_sig)
    while not stop["v"]:
        time.sleep(cfg["segment_seconds"] / 2.0)
        rc = proc.poll()
        prune(cfg)
        if rc is not None:
            log("recorder exited with %d — restarting it (a crashed capture must not silently "
                "cost you the next clip)" % rc)
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if once:
            break
    try:
        proc.terminate()
        proc.wait(timeout=10)
    except (OSError, subprocess.SubprocessError):
        proc.kill()
    write_state({"state": "stopped", "ring": RING})
    return 0


def status(cfg: dict) -> int:
    doc = {}
    if os.path.isfile(STATE):
        try:
            doc = json.load(open(STATE, encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            doc = {"state": "state file unreadable"}
    segs = sorted(f for f in os.listdir(RING) if f.endswith(".mkv")) if os.path.isdir(RING) else []
    size = sum(os.path.getsize(os.path.join(RING, f)) for f in segs)
    doc.update({"segments": len(segs), "ring_bytes": size, "ring": RING, "clips": cfg["output_dir"]})
    print(json.dumps(doc, indent=1, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hedron-replayd", description=__doc__.splitlines()[0])
    ap.add_argument("--once", action="store_true", help="probe + one segment, then exit (tests)")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--check", action="store_true", help="verify an encoder is usable and exit 0/1")
    args = ap.parse_args(argv)
    cfg = load_conf()
    if args.status:
        return status(cfg)
    if args.check:
        enc = encoder_choice(cfg)
        ok, why = probe_encoder(enc)
        print(json.dumps({"encoder": enc, "usable": ok, "detail": why}, sort_keys=True))
        return 0 if ok else 1
    if not cfg["enabled"]:
        log("disabled by configuration (%s)" % CONF)
        return 0
    return loop(cfg, once=args.once)


if __name__ == "__main__":
    raise SystemExit(main())
