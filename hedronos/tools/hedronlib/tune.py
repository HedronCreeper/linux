# SPDX-License-Identifier: MIT
"""FlowState — HedronOS's transactional performance-profile engine.

A profile is a *declarative patch* to the machine: sysctls, per-CPU governors and EPP,
cpuidle state disabling, GPU power profile, IRQ affinity, THP, swap layout, RT limits,
scheduler choice. What makes it different from "a pile of shell scripts in a dotfiles
repo" is three properties enforced here:

1. **Transaction.** `render()` computes the full desired state; `apply()` diffs it
   against the live state, writes only what differs, and returns the exact inverse so
   any switch can be rolled back even after a crash (the journal is on disk).
2. **Composability.** Profiles inherit (`gaming` → `balanced` → `base`), hardware
   overlays apply on top (NVIDIA needs a different GPU knob than AMD; a handheld needs
   power caps), and user drops-in override. Conflicts are detected, not lost.
3. **Testability.** Everything works against a *fake* sysfs tree (tests/fixtures/sysfs),
   which means the whole engine — including error paths for read-only knobs — is
   exercised offline in `make test`, not on someone's gaming rig.
"""

from __future__ import annotations

import fnmatch
import glob
import os
from dataclasses import dataclass, field
from typing import Any, Iterable

from .util import as_list, deep_merge, load_toml, to_bool

PROFILE_ORDER = ("battery", "silence", "balanced", "gaming", "performance", "handheld", "studio")
KINDS = ("sysctl", "sysfs", "cpu", "gpu", "irq", "sched", "services", "env", "limits", "extra")


@dataclass
class Action:
    """One concrete write to the system (or its rejection, with a reason)."""

    kind: str
    path: str
    value: str
    current: str | None = None
    mode: str = "write"            # write | list | command | skip
    reason: str = ""
    rollback: str | None = None
    volatile: bool = False         # lost on reboot unless a unit re-applies it
    profile: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "path": self.path, "value": self.value, "current": self.current,
                "mode": self.mode, "reason": self.reason, "rollback": self.rollback,
                "volatile": self.volatile, "profile": self.profile}

    def describe(self) -> str:
        if self.mode == "skip":
            return "skip  %-42s %s" % (self.path, self.reason)
        arrow = "=" if self.mode == "write" else ":"
        cur = "" if self.current is None else " (was %s)" % self.current
        return "%-6s %-42s %s%s%s" % (self.kind, self.path, arrow, self.value, cur)


@dataclass
class Transaction:
    profile: str
    actions: list[Action] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)

    @property
    def changes(self) -> list[Action]:
        return [a for a in self.actions if a.mode != "skip"]

    def as_dict(self) -> dict[str, Any]:
        return {"profile": self.profile, "actions": [a.as_dict() for a in self.actions],
                "notes": list(self.notes), "conflicts": list(self.conflicts),
                "changed": len(self.changes), "total": len(self.actions)}


class ProfileError(Exception):
    pass


# ------------------------------------------------------------------ fake sysfs
class Sysfs:
    """Read/write against a root prefix so tests can point at a fixture tree."""

    def __init__(self, root: str = "/"):
        self.root = root

    def path(self, rel: str) -> str:
        rel = rel.lstrip("/")
        return rel if os.path.isabs(rel) and self.root == "/" else os.path.join(self.root, rel)

    def read(self, rel: str) -> str | None:
        try:
            with open(self.path(rel), encoding="utf-8", errors="replace") as fh:
                return fh.read().strip()
        except OSError:
            return None

    def write(self, rel: str, value: str) -> tuple[bool, str]:
        path = self.path(rel)
        if not os.path.exists(path):
            return False, "no such file: %s" % path
        if os.access(path, os.W_OK) is False:
            return False, "not writable (read-only or missing privilege)"
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(value)
        except OSError as exc:
            return False, str(exc)
        return True, ""

    def glob(self, pattern: str) -> list[str]:
        return sorted(glob.glob(self.path(pattern)))


# ------------------------------------------------------------------- data model
class ProfileSet:
    def __init__(self, directory: str):
        self.directory = directory
        self.profiles: dict[str, dict[str, Any]] = {}
        self.overlays: dict[str, dict[str, Any]] = {}
        self.load()

    def load(self) -> None:
        for path in sorted(glob.glob(os.path.join(self.directory, "*.toml"))):
            try:
                data = load_toml(path)
            except Exception as exc:  # noqa: BLE001 - a broken drop-in must not brick boot
                raise ProfileError("%s: %s" % (os.path.basename(path), exc)) from exc
            name = data.get("profile", {}).get("name") or os.path.splitext(os.path.basename(path))[0]
            if data.get("overlay"):
                self.overlays[name] = data
            else:
                self.profiles[name] = data
        if not self.profiles:
            raise ProfileError("no profiles found in %s" % self.directory)

    def names(self) -> list[str]:
        return sorted(self.profiles)

    # ------------------------------------------------------------------ resolve
    def resolve(self, name: str, *, hardware: Iterable[str] = (), user_drops: str | None = None,
                _seen: set[str] | None = None) -> dict[str, Any]:
        """Merge base → inherited → hardware overlays → user drop-ins."""
        _seen = _seen or set()
        if name in _seen:
            raise ProfileError("profile inheritance cycle at %s" % name)
        _seen.add(name)
        spec = self.profiles.get(name)
        if spec is None:
            raise ProfileError("unknown profile %r (known: %s)" % (name, ", ".join(self.names())))
        merged: dict[str, Any] = {}
        parent = spec.get("profile", {}).get("inherits")
        if parent:
            merged = self.resolve(parent, hardware=hardware, user_drops=None, _seen=set(_seen))
        merged = deep_merge(merged, {k: v for k, v in spec.items() if k != "profile"})
        merged.setdefault("profile", {})["name"] = name
        for hw in hardware:
            overlay = self.overlays.get(hw)
            if overlay:
                merged = deep_merge(merged, {k: v for k, v in overlay.items() if k != "profile"})
                merged.setdefault("profile", {}).setdefault("overlays", []).append(hw)
        if user_drops and os.path.isdir(user_drops):
            for path in sorted(glob.glob(os.path.join(user_drops, "%s.*.toml" % name))):
                try:
                    merged = deep_merge(merged, load_toml(path))
                    merged.setdefault("profile", {}).setdefault("user_drops", []).append(os.path.basename(path))
                except Exception as exc:  # noqa: BLE001
                    raise ProfileError("user drop-in %s: %s" % (os.path.basename(path), exc)) from exc
        return merged

    # ---------------------------------------------------------------- render
    def render(self, name: str, *, sysfs: Sysfs, detect: "Detect", include_unchanged: bool = True) -> Transaction:
        spec = self.resolve(name, hardware=detect.overlays, user_drops=detect.user_drop_dir)
        tx = Transaction(profile=name)
        want_profile = spec.get("profile", {})

        for section in KINDS:
            body = spec.get(section, {}) or {}
            handler = getattr(self, "_render_" + section, None)
            if handler is None:
                tx.conflicts.append("unknown section %r in profile %s" % (section, name))
                continue
            handler(body, tx, sysfs, detect, name)

        note = want_profile.get("note")
        if note:
            tx.notes.append(str(note))
        if want_profile.get("overlays"):
            tx.notes.append("hardware overlays: " + ", ".join(want_profile["overlays"]))
        if want_profile.get("user_drops"):
            tx.notes.append("user drop-ins: " + ", ".join(want_profile["user_drops"]))
        if not include_unchanged:
            tx.actions = [a for a in tx.actions if a.mode != "skip"]
        return tx

    # -- section renderers
    def _render_sysctl(self, body: dict, tx: Transaction, sysfs: Sysfs, detect: Detect, profile: str) -> None:
        for key, value in sorted(body.items()):
            path = "proc/sys/%s" % str(key).replace(".", "/")
            current = sysfs.read(path)
            want = str(value)
            if current is None:
                tx.actions.append(Action("sysctl", str(key), want, None, "skip",
                                          reason="kernel knob absent (module not loaded?)", profile=profile))
                continue
            same = current.split() == want.split()
            tx.actions.append(Action("sysctl", str(key), want, current, "write" if not same else "skip",
                                      reason="already set" if same else "",
                                      rollback=current, volatile=True, profile=profile))

    def _render_sysfs(self, body: dict, tx: Transaction, sysfs: Sysfs, detect: Detect, profile: str) -> None:
        for pattern, value in sorted(body.items()):
            matched = sysfs.glob(pattern)
            if not matched:
                tx.actions.append(Action("sysfs", pattern, str(value), None, "skip",
                                          reason="no matching path on this hardware", profile=profile))
                continue
            for path in matched:
                rel = os.path.relpath(path, sysfs.root) if sysfs.root != "/" else path
                current = sysfs.read(rel)
                want = str(value)
                if current is not None and _list_contains(current, want):
                    tx.actions.append(Action("sysfs", rel, want, current, "skip", reason="already set",
                                              profile=profile))
                else:
                    tx.actions.append(Action("sysfs", rel, want, current, "write", rollback=current,
                                             volatile=True, profile=profile))

    def _render_cpu(self, body: dict, tx: Transaction, sysfs: Sysfs, detect: Detect, profile: str) -> None:
        governor = body.get("governor")
        epp = body.get("energy_performance_preference")
        epa = body.get("energy_performance_available_preferences")
        boost = body.get("boost")
        hwp = body.get("hwp_dynamic")
        ids = detect.cpu_ids()
        for cpu in ids:
            if governor:
                path = "sys/devices/system/cpu/cpu%d/cpufreq/scaling_governor" % cpu
                current = sysfs.read(path)
                if current is None:
                    tx.notes.append("cpu%d: no cpufreq driver (skip governor)" % cpu)
                elif current != governor:
                    tx.actions.append(Action("cpu", path, governor, current, "write", rollback=current,
                                             volatile=True, profile=profile))
            if epp:
                path = "sys/devices/system/cpu/cpu%d/cpufreq/energy_performance_preference" % cpu
                current = sysfs.read(path)
                available = sysfs.read("sys/devices/system/cpu/cpu%d/cpufreq/"
                                       "energy_performance_available_preferences" % cpu) or ""
                if current is None:
                    pass
                elif epa and not any(fnmatch.fnmatch(t, epa) for t in available.split()):
                    tx.actions.append(Action("cpu", path, epa, current, "skip",
                                              reason="this CPU offers only: " + (available or "nothing"),
                                              profile=profile))
                elif current != epp:
                    tx.actions.append(Action("cpu", path, epp, current, "write", rollback=current,
                                             volatile=True, profile=profile))
            for state in as_list(body.get("disable_idle_states", [])):
                path = "sys/devices/system/cpu/cpu%d/cpuidle/state%s/disable" % (cpu, state)
                current = sysfs.read(path)
                if current is None:
                    continue
                if current.strip() != "1":
                    tx.actions.append(Action("cpu", path, "1", current, "write", rollback=current,
                                             volatile=True, profile=profile))
        if boost is not None:
            path = "sys/devices/system/cpu/cpufreq/boost"
            current = sysfs.read(path)
            want = "1" if to_bool(boost) else "0"
            if current is None:
                tx.notes.append("no global boost knob (older AMD P-State or intel_pstate disabled)")
            elif current.strip() != want:
                tx.actions.append(Action("cpu", path, want, current, "write", rollback=current,
                                          volatile=True, profile=profile))
        if hwp is not None:
            for cpu in ids:
                path = "sys/devices/system/cpu/cpu%d/cpufreq/hwp_dynamic_integrity" % cpu
                if sysfs.read(path) is not None:
                    tx.actions.append(Action("cpu", path, "1" if to_bool(hwp) else "0", None, "write",
                                             volatile=True, profile=profile))

    def _render_gpu(self, body: dict, tx: Transaction, sysfs: Sysfs, detect: Detect, profile: str) -> None:
        for key, value in sorted(body.items()):
            if key == "waitvsync_policy":
                continue
            patterns = detect.gpu_paths(key)
            if not patterns:
                tx.actions.append(Action("gpu", key, str(value), None, "skip",
                                          reason="no GPU exposing %s on this machine" % key, profile=profile))
                continue
            for path in patterns:
                current = sysfs.read(path)
                want = str(value)
                if current is None:
                    continue
                if _list_contains(current, want):
                    tx.actions.append(Action("gpu", path, want, current, "skip", reason="already set",
                                             profile=profile))
                else:
                    tx.actions.append(Action("gpu", path, want, current, "write", rollback=current,
                                             volatile=True, profile=profile))

    def _render_irq(self, body: dict, tx: Transaction, sysfs: Sysfs, detect: Detect, profile: str) -> None:
        if not body:
            return
        pattern = body.get("isolate_irq_pattern")
        cpus = body.get("cpus")
        if pattern is None or cpus is None:
            tx.conflicts.append("irq section needs isolate_irq_pattern and cpus")
            return
        mask = detect.cpumask(cpus)
        if not os.path.isfile(sysfs.path("proc/interrupts")):
            tx.actions.append(Action("irq", "proc/interrupts", mask, None, "skip",
                                      reason="/proc/interrupts unavailable", profile=profile))
            return
        count = 0
        for line in (sysfs.read("proc/interrupts") or "").splitlines()[1:]:
            fields = line.split()
            if not fields or not fields[0].endswith(":"):
                continue
            irq = fields[0][:-1]
            label = " ".join(fields[-3:]) if len(fields) > 3 else ""
            if not fnmatch.fnmatch(label.lower(), str(pattern).lower()):
                continue
            path = "proc/irq/%s/smp_affinity_list" % irq
            current = sysfs.read(path)
            if current is None or current.strip() == str(cpus):
                continue
            tx.actions.append(Action("irq", path, str(cpus), current, "write", rollback=current,
                                     volatile=True, profile=profile))
            count += 1
        if count == 0:
            tx.notes.append("no IRQs matched %r (nothing to isolate)" % pattern)

    def _render_sched(self, body: dict, tx: Transaction, sysfs: Sysfs, detect: Detect, profile: str) -> None:
        scheduler = body.get("scheduler")
        if scheduler:
            available = (sysfs.read("sys/fs/cgroup/cpuset/cpuset.cpus") or "")
            path = "sys/module/hedron_sched/parameters/mode"
            current = sysfs.read(path)
            if current is None:
                tx.actions.append(Action("sched", "scheduler", scheduler, None, "skip",
                                          reason="scx_hedron not loaded (kernel without CONFIG_SCHED_CLASS_EXT, "
                                                 "or hedron-scx disabled)", profile=profile))
            elif current != scheduler:
                tx.actions.append(Action("sched", path, scheduler, current, "write", rollback=current,
                                         volatile=True, profile=profile))
            if available:
                tx.notes.append("cpuset.cpus=%s" % available)
        for key, value in sorted((body.get("cgroup", {}) or {}).items()):
            path = "sys/fs/cgroup/user.slice/%s" % key
            current = sysfs.read(path)
            if current is not None and current.strip() != str(value):
                tx.actions.append(Action("sched", path, str(value), current, "write", rollback=current,
                                         volatile=True, profile=profile))

    def _render_services(self, body: dict, tx: Transaction, sysfs: Sysfs, detect: Detect, profile: str) -> None:
        for action, units in sorted(body.items()):
            if action not in ("enable", "disable", "restart", "mask"):
                tx.conflicts.append("services: unknown action %r" % action)
                continue
            for unit in as_list(units):
                tx.actions.append(Action("services", "%s:%s" % (action, unit), "", None, "command",
                                         reason="profile %s" % profile, profile=profile))

    def _render_env(self, body: dict, tx: Transaction, sysfs: Sysfs, detect: Detect, profile: str) -> None:
        if not body:
            return
        path = "etc/environment.d/50-hedron-flowstate.conf"
        want = "\n".join("%s=%s" % (k, v) for k, v in sorted(body.items()))
        current = sysfs.read(path)
        if current == want + "\n" or current == want:
            tx.actions.append(Action("env", path, "…", current, "skip", reason="already set", profile=profile))
        else:
            tx.actions.append(Action("env", path, want, current, "write", rollback=current, profile=profile))

    def _render_limits(self, body: dict, tx: Transaction, sysfs: Sysfs, detect: Detect, profile: str) -> None:
        if not body:
            return
        path = "etc/security/limits.d/99-hedron-%s.conf" % profile
        lines = ["# HedronOS FlowState limits (generated by hedron-tune)"]
        for domain, items in sorted(body.items()):
            for item, value in sorted((items or {}).items()):
                lines.append("%s soft %s %s" % (domain, item, value))
                lines.append("%s hard %s %s" % (domain, item, value))
        want = "\n".join(lines) + "\n"
        current = sysfs.read(path)
        if current == want:
            tx.actions.append(Action("limits", path, "…", current, "skip", reason="already set", profile=profile))
        else:
            tx.actions.append(Action("limits", path, want, current, "write", rollback=current, profile=profile))

    def _render_extra(self, body: dict, tx: Transaction, sysfs: Sysfs, detect: Detect, profile: str) -> None:
        for name, spec in sorted((body or {}).items()):
            if isinstance(spec, str):
                cmd, mode = spec, "command"
            else:
                cmd = str((spec or {}).get("command", ""))
                mode = str((spec or {}).get("mode", "command"))
            if not cmd:
                tx.conflicts.append("extra.%s has no command" % name)
                continue
            tx.actions.append(Action("extra", name, cmd, None, mode,
                                      reason="profile %s" % profile, profile=profile))


def _list_contains(current: str, want: str) -> bool:
    text = current.strip()
    if text == want.strip():
        return True
    # many sysfs knobs report "[value]" inside a candidate list, or accept a token that
    # is already one of the words (e.g. "1" inside "[1] 0").
    if ("[%s]" % want.strip()) in text:
        return True
    return want.strip() in text.split()


class Detect:
    """Hardware facts that decide which overlays apply and where knobs live."""

    def __init__(self, sysfs: Sysfs, info: Any = None, vendor: str = "amd",
                 handheld: bool = False, laptop: bool = False, user_drop_dir: str | None = None):
        self.sysfs = sysfs
        self.info = info
        self.vendor = vendor
        self.handheld = handheld
        self.laptop = laptop
        self.user_drop_dir = user_drop_dir

    PCI_TO_VENDOR = {0x10DE: "nvidia", 0x1002: "amd", 0x8086: "intel"}

    @classmethod
    def from_system(cls, root: str = "/", info: Any = None, user_drop_dir: str | None = None) -> "Detect":
        """Prefer the PCI probe (authoritative); fall back to which KMS module is loaded."""
        sysfs = Sysfs(root)
        vendor, handheld, laptop = "unknown", False, False
        if info is not None:
            gpus = [d for d in info.devices if d.is_gpu]
            if gpus:
                vendor = cls.PCI_TO_VENDOR.get(gpus[0].vendor, "generic")
            handheld, laptop = info.is_handheld(), info.is_laptop()
        else:
            for name, mapped in (("nvidia", "nvidia"), ("amdgpu", "amd"),
                                 ("i915", "intel"), ("xe", "intel")):
                if os.path.isdir(sysfs.path("sys/module/%s" % name)):
                    vendor = mapped
                    break
            else:
                vendor = "generic"
        return cls(sysfs, info, vendor=vendor, handheld=handheld, laptop=laptop,
                   user_drop_dir=user_drop_dir)

    @property
    def overlays(self) -> list[str]:
        out = [self.vendor]
        if self.handheld:
            out.append("handheld")
        if self.laptop:
            out.append("laptop")
        return out

    def cpu_count(self) -> int:
        text = self.sysfs.read("sys/devices/system/cpu/possible") or "0"
        try:
            high = text.split("-")[-1]
            return int(high) + 1
        except ValueError:
            return 1

    def cpu_ids(self) -> list[int]:
        ids = []
        for path in self.sysfs.glob("sys/devices/system/cpu/cpu[0-9]*"):
            name = os.path.basename(path)
            if name.startswith("cpu") and name[3:].isdigit():
                ids.append(int(name[3:]))
        return ids or list(range(self.cpu_count()))

    def performance_cpus(self) -> list[int]:
        """Big cores, from the kernel's own frequency ranking (falls back to all CPUs)."""
        freqs = {}
        for cpu in self.cpu_ids():
            raw = self.sysfs.read("sys/devices/system/cpu/cpu%d/cpufreq/cpuinfo_max_freq" % cpu)
            if raw and raw.isdigit():
                freqs[cpu] = int(raw)
        if not freqs:
            return self.cpu_ids()
        top = max(freqs.values())
        return sorted(c for c, f in freqs.items() if f >= top - 1)

    def cpumask(self, spec: Any) -> str:
        if isinstance(spec, str) and spec.isdigit():
            return spec
        if spec == "performance":
            ids = self.performance_cpus()
            mask = 0
            for cpu in ids:
                mask |= 1 << cpu
            return _mask_to_hex(mask)
        if isinstance(spec, (list, tuple)):
            mask = 0
            for cpu in spec:
                mask |= 1 << int(cpu)
            return _mask_to_hex(mask)
        return "0"

    def gpu_paths(self, knob: str) -> list[str]:
        """Where a named GPU knob actually lives on this vendor's driver."""
        cards = self.sysfs.glob("sys/class/drm/card[0-9]")
        out: list[str] = []
        for card in cards:
            base = card + "/device"
            candidates = {
                "power_profile": ["power_dpm_force_performance_level", "power/power_profile"],
                "gpu_clock_lock": ["pp_dpm_sclk"],
                "mem_clock_lock": ["pp_dpm_mclk"],
                "fan_curve": ["pwm1", "hwmon/hwmon*/pwm1"],
                "power_cap": ["power_cap_usd", "hwmon/hwmon*/power1_cap"],
                "power_minimum": ["power_minimum_freq", "hwmon/hwmon*/power1_min"],
                "vbo": ["vbios_version"],
                "nvidia_power": ["../nvidia-power"],
            }.get(knob, [knob])
            for cand in candidates:
                for path in sorted(glob.glob(os.path.join(base, cand))):
                    out.append(os.path.relpath(path, self.sysfs.root) if self.sysfs.root != "/"
                               else path)
        if knob == "nvidia_boost" and self.vendor == "nvidia":
            out.append("proc/driver/nvidia/params")
        return out


def _mask_to_hex(mask: int) -> str:
    if mask == 0:
        return "0"
    hexstr = "%x" % mask
    groups = []
    while hexstr:
        groups.append(hexstr[-8:])
        hexstr = hexstr[:-8]
    return ",".join(groups)


# ---------------------------------------------------------------------- status
def status(sysfs: Sysfs, detect: Detect) -> dict[str, Any]:
    """What the machine is doing right now — the HUD and `hedron-tune status` share this."""
    out: dict[str, Any] = {"cpus": {}, "sysctl": {}, "gpu": {}, "notes": []}
    for cpu in detect.cpu_ids()[:16]:
        gov = sysfs.read("sys/devices/system/cpu/cpu%d/cpufreq/scaling_governor" % cpu)
        cur = sysfs.read("sys/devices/system/cpu/cpu%d/cpufreq/scaling_cur_freq" % cpu)
        epp = sysfs.read("sys/devices/system/cpu/cpu%d/cpufreq/energy_performance_preference" % cpu)
        out["cpus"][cpu] = {"governor": gov, "freq_khz": cur, "epp": epp}
    for key in ("kernel/sched_rt_runtime_us", "vm/swappiness", "vm/dirty_expire_centisecs",
                "vm/dirty_writeback_centisecs", "kernel/sched_cfs_bandwidth_slice_us",
                "net/core/busy_poll", "net/ipv4/tcp_no_metrics_save"):
        path = "proc/sys/%s" % key.replace(".", "/")
        value = sysfs.read(path)
        if value is not None:
            out["sysctl"][key] = value
    for knob in ("power_profile", "gpu_clock_lock", "power_cap"):
        paths = detect.gpu_paths(knob)
        for path in paths[:2]:
            value = sysfs.read(path)
            if value is not None:
                out["gpu"][knob] = {"path": path, "value": value[:120]}
    if not out["cpus"]:
        out["notes"].append("no cpufreq sysfs visible (container or missing driver)")
    return out


def active_profile(sysfs: Sysfs) -> str | None:
    marker = sysfs.read("var/lib/hedron/flowstate/active")
    return marker.strip() if marker else None
