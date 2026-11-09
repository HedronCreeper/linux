#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""hedron-vitalsd — one sampler for temperatures, clocks, frame pacing and power.

Every HUD in the ecosystem (MangoHud, gamescope's OSD, CoreCtrl, nvtop) samples the same
sysfs files independently, several times per second, each in its own process. On a
handheld that is a measurable part of the idle power budget, and on desktops it is how you
end up with three different CPU temperatures on screen at once because they each read a
different hwmon chip.

So HedronOS has *one* sampler. It reads once per tick, keeps an EWMA plus a small ring of
raw samples, and publishes the result as a single JSON document on a UNIX socket. The overlay,
`hedron-vitals`, `hedron-doctor` and the FlowState thermal rule are all clients of it.

Contract: the socket always answers instantly with the last published sample (never blocks on
a read of /sys), and the sample carries `stale` so a client can show a grey HUD instead of a
lying one.
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import socket
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in ("/usr/lib/python3.13/site-packages", os.path.join(_HERE, "..", "python3.13/site-packages")):
    if os.path.isdir(os.path.join(_cand, "hedronlib")) and _cand not in sys.path:
        sys.path.insert(0, os.path.abspath(_cand))
        break

SOCK = os.environ.get("HEDRON_VITALS_SOCK") or "/run/hedron/vitals.sock"
LATEST = "/run/hedron/vitals.json"

# ------------------------------------------------------------------ sysfs readers
def read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return None


def read_int(path: str) -> int | None:
    raw = read(path)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def hwmon_sensors() -> dict[str, dict[str, float]]:
    """Every hwmon chip, labelled by what it actually is (name + labels)."""
    out: dict[str, dict[str, float]] = {}
    for chip in sorted(glob.glob("/sys/class/hwmon/hwmon*")):
        name = read(os.path.join(chip, "name")) or os.path.basename(chip)
        entry: dict[str, float] = {}
        for path in sorted(glob.glob(os.path.join(chip, "temp*_input"))):
            label = read(path.replace("_input", "_label")) or os.path.basename(path).split("_")[0]
            value = read_int(path)
            if value is not None:
                entry[label] = value / 1000.0
        for path in sorted(glob.glob(os.path.join(chip, "in*_input"))):
            label = os.path.basename(path).split("_")[0]
            value = read_int(path)
            if value is not None:
                entry["volt_" + label] = value / 1000.0
        for key, factor in (("power1_input", 1e6), ("power1_average", 1e6), ("energy1_input", 1e6)):
            value = read_int(os.path.join(chip, key))
            if value is not None:
                entry[key.split("_")[0]] = value / factor
        for path in sorted(glob.glob(os.path.join(chip, "fan*_input"))):
            label = os.path.basename(path).split("_")[0]
            value = read_int(path)
            if value is not None:
                entry["fan_" + label] = float(value)
        if entry:
            out.setdefault(name, {}).update(entry)
    return out


def gpu_busy() -> dict[str, float]:
    """Utilisation from the DRM scheduler, which is the only honest vendor-neutral number."""
    out: dict[str, float] = {}
    for path in sorted(glob.glob("/sys/class/drm/card*/device/gpu_busy_percent")):
        value = read_int(path)
        if value is not None:
            out["percent"] = float(value)
            return out
    # nvidia has no sysfs; nvidia-smi is the documented interface and it is cheap enough at
    # one call per tick (we are not polling it at 120 Hz like some HUDs do).
    if os.path.exists("/proc/driver/nvidia"):
        try:
            import subprocess

            proc = subprocess.run(["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,clocks.sm",
                                   "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=1.5)
            if proc.returncode == 0 and proc.stdout.strip():
                util, mem, clk = [p.strip() for p in proc.stdout.strip().split(",")[:3]]
                out["percent"] = float(util)
                out["vram_mib"] = float(mem)
                out["clock_mhz"] = float(clk)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    return out


def cpu_freqs() -> dict[str, float]:
    cur, maxf = [], []
    for path in sorted(glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq")):
        value = read_int(path)
        if value:
            cur.append(value / 1000.0)
    for path in sorted(glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/cpuinfo_max_freq")):
        value = read_int(path)
        if value:
            maxf.append(value / 1000.0)
    if not cur:
        return {}
    return {"mhz_avg": sum(cur) / len(cur), "mhz_max": max(cur), "boost_pct": (sum(cur) / len(cur)) /
            (sum(maxf) / len(maxf)) * 100.0 if maxf else 0.0, "cores": float(len(cur))}


def loadavg() -> dict[str, float]:
    raw = read("/proc/loadavg")
    if not raw:
        return {}
    a, b, c = raw.split()[:3]
    return {"m1": float(a), "m5": float(b), "m15": float(c)}


def memory() -> dict[str, float]:
    out: dict[str, float] = {}
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                key, _, rest = line.partition(":")
                kib = rest.strip().split()[0]
                if key in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree", "Zswapped"):
                    out[key] = float(kib) / 1048576.0
    except (OSError, IndexError, ValueError):
        return {}
    if "MemTotal" in out and "MemAvailable" in out:
        out["used"] = out["MemTotal"] - out["MemAvailable"]
        out["used_pct"] = out["used"] / out["MemTotal"] * 100.0 if out["MemTotal"] else 0.0
    return out


def psi() -> dict[str, float]:
    """Pressure-stall information: the only metric that tells you *why* frames are late."""
    out: dict[str, float] = {}
    for kind in ("cpu", "io", "memory"):
        raw = read("/proc/pressure/%s" % kind)
        if not raw:
            continue
        some = raw.split()[0]
        try:
            out["some_" + kind] = float(some.split("=")[1])
        except (IndexError, ValueError):
            continue
    return out


def game_fps() -> dict[str, float]:
    """Frame stats published by the scheduler/overlay, if any game is being tracked.

    scx_hedron exports a per-cgroup frame counter through /sys/kernel/debug (see
    tools/sched_ext); MangoHud can write an IPC file. Both are optional by design.
    """
    for path in ("/run/hedron/fps.json", "/tmp/mango-hud-last.json"):
        raw = read(path)
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        out = {}
        for key in ("fps", "frametime_ms", "p95_ms", "p99_ms", "frame_time", "fps_avg"):
            if key in data:
                try:
                    out[{"frame_time": "frametime_ms", "fps_avg": "fps"}.get(key, key)] = float(data[key])
                except (TypeError, ValueError):
                    continue
        if out:
            return out
    return {}


def battery() -> dict[str, float]:
    out: dict[str, float] = {}
    for path in sorted(glob.glob("/sys/class/power_supply/BAT*")):
        cap = read_int(os.path.join(path, "capacity"))
        status = read(os.path.join(path, "status"))
        watts = read_int(os.path.join(path, "power_now")) or read_int(os.path.join(path, "current_now"))
        volt = read_int(os.path.join(path, "voltage_now"))
        if cap is not None:
            out["percent"] = float(cap)
        if status:
            out["status"] = status
        if watts is not None and volt:
            out["watts"] = watts / 1e6 if watts > 1e5 else (watts * volt) / 1e12
    return out


def fans() -> dict[str, float]:
    out: dict[str, float] = {}
    for path in sorted(glob.glob("/sys/class/hwmon/hwmon*/pwm*")):
        if path.endswith("_enable"):
            continue
        value = read_int(path)
        if value is not None:
            out[os.path.basename(path)] = value / 255.0 * 100.0
    return out


# --------------------------------------------------------------------- the sampler
class Sampler(threading.Thread):
    def __init__(self, interval: float = 0.5):
        super().__init__(daemon=True)
        self.interval = interval
        self.stop_flag = threading.Event()
        self.latest: dict = {"state": "starting", "t": time.time()}
        self.ewma: dict[str, float] = {}

    def sample(self) -> dict:
        sensors = hwmon_sensors()
        temps: dict[str, float] = {}
        for chip, values in sensors.items():
            for label, value in values.items():
                if label.startswith(("temp", "Package", "Tctl", "Tdie", "edge", "junction")) or "°" in label:
                    temps["%s/%s" % (chip, label)] = value
                elif not label.startswith("volt"):
                    temps["%s/%s" % (chip, label)] = value
        hottest = max((v for k, v in temps.items() if not k.endswith(("/volt_sensor",))), default=0.0)
        power = sum(v for chip in sensors.values() for k, v in chip.items() if k.startswith("power"))
        snap = {
            "t": time.time(),
            "temp_c": round(hottest, 1),
            "temps": {k: round(v, 1) for k, v in sorted(temps.items())[:24]},
            "power_w": round(power, 1),
            "gpu": gpu_busy(),
            "cpu": cpu_freqs(),
            "load": loadavg(),
            "mem": memory(),
            "psi": psi(),
            "fans": {k: round(v, 1) for k, v in fans().items()},
            "battery": battery(),
            "game": game_fps(),
            "stale": False,
        }
        # EWMA so a HUD number does not jump 12 % between ticks
        for key, value in (("temp_c", snap["temp_c"]), ("power_w", snap["power_w"]),
                           ("gpu_busy", snap["gpu"].get("percent", 0.0)),
                           ("fps", snap["game"].get("fps", 0.0))):
            prev = self.ewma.get(key)
            self.ewma[key] = value if prev is None else prev * 0.7 + value * 0.3
        snap["ewma"] = {k: round(v, 2) for k, v in self.ewma.items()}
        return snap

    def run(self) -> None:
        while not self.stop_flag.is_set():
            started = time.perf_counter()
            try:
                self.latest = self.sample()
            except Exception as exc:  # noqa: BLE001 - a HUD must never take the machine down
                self.latest = {"state": "error", "error": "%s: %s" % (type(exc).__name__, exc),
                               "t": time.time(), "stale": True}
            publish(self.latest)
            delay = self.interval - (time.perf_counter() - started)
            if delay > 0:
                self.stop_flag.wait(delay)

    def once(self) -> dict:
        self.latest = self.sample()
        publish(self.latest)
        return self.latest


def publish(doc: dict) -> None:
    try:
        os.makedirs(os.path.dirname(LATEST), exist_ok=True)
        tmp = LATEST + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, sort_keys=True)
        os.replace(tmp, LATEST)
    except OSError:
        pass


def serve(sampler: Sampler, one_shot: bool) -> int:
    try:
        os.makedirs(os.path.dirname(SOCK), exist_ok=True)
        if os.path.exists(SOCK):
            os.unlink(SOCK)
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(SOCK)
        listener.listen(8)
        os.chmod(SOCK, 0o666)
    except OSError as exc:
        print("hedron-vitalsd: cannot bind %s (%s); publishing to %s only" % (SOCK, exc, LATEST),
              file=sys.stderr)
        listener = None
    if one_shot:
        if listener:
            listener.close()
        return 0

    def accept_loop():
        assert listener is not None
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.sendall((json.dumps(sampler.latest, sort_keys=True) + "\n").encode())
                except OSError:
                    pass

    if listener:
        threading.Thread(target=accept_loop, daemon=True).start()
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        sampler.stop_flag.set()
        if listener:
            listener.close()
            try:
                os.unlink(SOCK)
            except OSError:
                pass
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hedron-vitalsd", description=__doc__.splitlines()[0])
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("--once", action="store_true", help="one sample, print it, exit (tests and CLI)")
    ap.add_argument("--pretty", action="store_true", help="with --once, print a human summary")
    ap.add_argument("--socket", default=None, help="override the socket path")
    args = ap.parse_args(argv)
    global SOCK
    if args.socket:
        SOCK = args.socket
    sampler = Sampler(args.interval)
    if args.once:
        doc = sampler.once()
        if args.pretty:
            print(format_summary(doc))
        else:
            print(json.dumps(doc, indent=1, sort_keys=True))
        return 0
    sampler.start()
    return serve(sampler, one_shot=False)


def format_summary(doc: dict) -> str:
    game = doc.get("game", {})
    lines = []
    if game:
        lines.append("fps      %.0f  (frametime %.2f ms, p95 %.2f, p99 %.2f)"
                     % (game.get("fps", 0), game.get("frametime_ms", 0), game.get("p95_ms", 0),
                        game.get("p99_ms", 0)))
    ewma = doc.get("ewma", {})
    lines.append("cpu      %s MHz avg (boost %.0f%%), %d cores"
                 % (("%.0f" % doc["cpu"]["mhz_avg"]) if doc.get("cpu") else "-",
                    doc["cpu"].get("boost_pct", 0) if doc.get("cpu") else 0,
                    int(doc["cpu"].get("cores", 0)) if doc.get("cpu") else 0))
    lines.append("gpu      %s%% busy%s" % (int(ewma.get("gpu_busy", 0)),
                 "" if not doc.get("gpu", {}).get("vram_mib") else
                 ", %.1f GiB VRAM" % (doc["gpu"]["vram_mib"] / 1024.0)))
    lines.append("temp     %.1f °C (hottest), %.1f W package" % (doc.get("temp_c", 0), doc.get("power_w", 0)))
    mem = doc.get("mem", {})
    if mem:
        lines.append("memory   %.1f/%.1f GiB (%.0f%%), swap %.1f/%.1f GiB"
                     % (mem.get("used", 0), mem.get("MemTotal", 0), mem.get("used_pct", 0),
                        mem.get("SwapTotal", 0) - mem.get("SwapFree", 0), mem.get("SwapTotal", 0)))
    psi = doc.get("psi", {})
    if psi:
        lines.append("pressure some%%: cpu %.1f  io %.1f  mem %.1f  (nonzero = something is waiting)"
                     % (psi.get("some_cpu", 0), psi.get("some_io", 0), psi.get("some_memory", 0)))
    load = doc.get("load", {})
    if load:
        lines.append("load     %.2f / %.2f / %.2f" % (load.get("m1", 0), load.get("m5", 0), load.get("m15", 0)))
    bat = doc.get("battery", {})
    if bat:
        lines.append("battery  %s%% %s%s" % (int(bat.get("percent", 0)), bat.get("status", "?"),
                                            ", %.1f W" % bat["watts"] if "watts" in bat else ""))
    fans = doc.get("fans", {})
    if fans:
        lines.append("fans     " + ", ".join("%s %.0f%%" % (k, v) for k, v in sorted(fans.items())[:4]))
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
