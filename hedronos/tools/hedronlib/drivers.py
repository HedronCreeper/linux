# SPDX-License-Identifier: MIT
"""The HedronOS driver decision engine.

One implementation, three callers:

* the **live ISO** calls it to pre-bake plans and to show the user "we found an
  RTX 4070 Ti → nvidia-open + these 9 packages + these 3 kernel parameters" *before*
  the install starts;
* the **installer** calls it and then executes :func:`install_actions` verbatim;
* the **installed system** runs it again from ``hedronos-drivers.service`` on every boot
  so a GPU swap, a new kernel, or a `rmmod` mistake self-heals.

Everything is pure (no subprocess, no writes) so it is unit-testable and auditable; the
caller performs the actions it is handed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .gpus import GpuDatabase, GpuIdentity, PciDevice, SystemInfo
from .util import as_list, norm_id, unique

PLAN_FORMAT = "hedronos-driver-plan/1"
ACTION_ORDER = ("write-file", "install-packages", "reload-initramfs", "enable-service",
                "set-env", "kernel-cmdline", "run", "verify")


@dataclass
class Package:
    name: str
    reason: str = ""
    tier: str = "required"          # required | recommended | optional
    source: str = "repo"            # repo | hedron | aur | flatpak

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "reason": self.reason, "tier": self.tier, "source": self.source}

    @staticmethod
    def parse(value: Any) -> "Package":
        if isinstance(value, Package):
            return value
        if isinstance(value, str):
            return Package(value)
        if isinstance(value, dict):
            return Package(value["name"], value.get("reason", ""), value.get("tier", "required"),
                           value.get("source", "repo"))
        raise TypeError(f"cannot interpret package spec {value!r}")


@dataclass
class DriverPlan:
    """What we will do for one machine."""

    vendor: str
    family: str
    branch: str = ""
    slot: str = ""
    kmod: str = ""
    title: str = ""
    confidence: float = 1.0
    packages: list[Package] = field(default_factory=list)
    firmware_packages: list[Package] = field(default_factory=list)
    cmdline: list[str] = field(default_factory=list)
    modprobe_files: dict[str, str] = field(default_factory=dict)
    initcpio_modules: list[str] = field(default_factory=list)
    initcpio_hooks: list[str] = field(default_factory=list)
    files: dict[str, str] = field(default_factory=dict)
    services_enable: list[str] = field(default_factory=list)
    services_mask: list[str] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)
    post_install: list[str] = field(default_factory=list)
    checks: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fallback: dict[str, Any] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------- output
    def package_names(self, tiers: Iterable[str] | None = None) -> list[str]:
        wanted = set(tiers) if tiers else None
        out = [p.name for p in self.packages if wanted is None or p.tier in wanted]
        out += [p.name for p in self.firmware_packages if wanted is None or p.tier in wanted]
        return unique(out)

    def as_dict(self) -> dict[str, Any]:
        return {
            "format": PLAN_FORMAT,
            "vendor": self.vendor,
            "family": self.family,
            "branch": self.branch,
            "slot": self.slot,
            "kmod": self.kmod,
            "title": self.title,
            "confidence": round(self.confidence, 3),
            "packages": [p.as_dict() for p in self.packages],
            "firmware_packages": [p.as_dict() for p in self.firmware_packages],
            "cmdline": list(self.cmdline),
            "modprobe_files": dict(self.modprobe_files),
            "initcpio_modules": list(self.initcpio_modules),
            "initcpio_hooks": list(self.initcpio_hooks),
            "files": dict(self.files),
            "services": {"enable": list(self.services_enable), "mask": list(self.services_mask)},
            "environment": dict(self.environment),
            "post_install": list(self.post_install),
            "checks": list(self.checks),
            "warnings": list(self.warnings),
            "fallback": dict(self.fallback),
            "extra": dict(self.extra),
        }


class RulesError(Exception):
    pass


class DriverEngine:
    def __init__(self, db: GpuDatabase, rules: dict[str, Any]):
        self.db = db
        self.rules = rules
        self.general = rules.get("general", {}) or {}
        self.vendors = rules.get("vendors", {}) or {}
        for name in ("common_packages", ):
            if name not in rules:
                raise RulesError(f"driver-rules.toml is missing top-level '{name}'")

    # ------------------------------------------------------------------ public
    def plan_for(self, info: SystemInfo, *, include_optional: bool = True) -> list[DriverPlan]:
        plans: list[DriverPlan] = []
        gpus = [d for d in info.devices if d.is_gpu]
        if not gpus:
            return [self._unknown_plan(info)]
        for dev in gpus:
            ident = self.db.lookup(dev)
            plans.append(self._plan_for_device(ident, info, include_optional=include_optional))
        # One machine, one merged plan is what the installer applies; keep per-GPU plans
        # for display and diagnostics.
        return plans

    def merged_plan(self, info: SystemInfo, *, include_optional: bool = True) -> DriverPlan:
        plans = self.plan_for(info, include_optional=include_optional)
        if len(plans) == 1:
            return plans[0]
        base = plans[0]
        merged = DriverPlan(
            vendor="+" + "+".join(sorted({p.vendor for p in plans})),
            family="+".join(p.family for p in plans),
            branch=base.branch,
            slot=";".join(p.slot for p in plans),
            kmod="+".join(p.kmod for p in plans if p.kmod),
            title=" + ".join(p.title for p in plans),
            confidence=min(p.confidence for p in plans),
        )
        for plan in plans:
            merged.packages.extend(plan.packages)
            merged.firmware_packages.extend(plan.firmware_packages)
            merged.cmdline.extend(plan.cmdline)
            merged.modprobe_files.update(plan.modprobe_files)
            merged.initcpio_modules.extend(plan.initcpio_modules)
            merged.initcpio_hooks.extend(plan.initcpio_hooks)
            merged.files.update(plan.files)
            merged.services_enable.extend(plan.services_enable)
            merged.services_mask.extend(plan.services_mask)
            merged.environment.update(plan.environment)
            merged.post_install.extend(plan.post_install)
            merged.checks.extend(plan.checks)
            merged.warnings.extend(plan.warnings)
        merged.packages = _dedupe_packages(merged.packages)
        merged.firmware_packages = _dedupe_packages(merged.firmware_packages)
        merged.cmdline = unique(merged.cmdline)
        merged.initcpio_modules = unique(merged.initcpio_modules)
        merged.initcpio_hooks = unique(merged.initcpio_hooks)
        merged.services_enable = unique(merged.services_enable)
        merged.services_mask = unique(merged.services_mask)
        merged.post_install = unique(merged.post_install)
        merged.warnings = unique(merged.warnings)
        vendors = {p.vendor for p in plans}
        if len(vendors) > 1:
            merged.warnings.append("hybrid graphics: rendering uses the discrete GPU while the "
                                   "display stays on the iGPU (PRIME offload enabled in session).")
        return merged

    # ----------------------------------------------------------------- internals
    def _plan_for_device(self, ident: GpuIdentity, info: SystemInfo, *,
                         include_optional: bool = True) -> DriverPlan:
        vrules = self.vendors.get(ident.vendor)
        if not vrules:
            return self._unknown_plan(info, ident=ident)
        branch = self._select_branch(vrules.get("branches", []), ident, info)
        if branch is None:
            return self._unknown_plan(info, ident=ident)

        branch_name = plan_branch_name(branch)
        plan = DriverPlan(
            vendor=ident.vendor,
            family=ident.family,
            branch=branch_name,
            slot=ident.device.slot,
            kmod=branch.get("kmod") or ident.kmod,
            title=branch.get("title") or "%s %s (%s)" % (ident.vendor, ident.family, branch_name),
            confidence=float(branch.get("confidence", 0.9 if ident.in_database else 0.6)),
        )
        tiers = {"required", "recommended"} | ({"optional"} if include_optional else set())

        for spec in as_list(branch.get("packages", [])):
            pkg = Package.parse(spec)
            if pkg.tier in tiers:
                plan.packages.append(pkg)
        for spec in as_list(self.rules.get("common_packages", [])):
            pkg = Package.parse(spec)
            if pkg.tier in tiers:
                plan.packages.append(pkg)
        for spec in as_list(branch.get("firmware_packages", [])):
            plan.firmware_packages.append(Package.parse(spec))
        if info.is_laptop() or info.hybrid:
            for spec in as_list(branch.get("laptop_packages", [])):
                plan.packages.append(Package.parse(spec))

        # Kernel parameters: branch, then machine-shape modifiers.
        plan.cmdline = unique(as_list(branch.get("cmdline", [])) + self._cmdline_for_machine(info, ident))
        plan.modprobe_files = _render_modprobe(branch, ident)
        plan.initcpio_modules = unique(as_list(branch.get("initcpio_modules", [])))
        plan.initcpio_hooks = unique(as_list(branch.get("initcpio_hooks", [])) +
                                    as_list(self.general.get("initcpio_hooks", [])))
        plan.files = {k: str(v) for k, v in (branch.get("files", {}) or {}).items()}
        plan.services_enable = unique(as_list(branch.get("services_enable", [])) +
                                      as_list(self.general.get("services_enable", [])))
        plan.services_mask = unique(as_list(branch.get("services_mask", [])))
        plan.environment = dict(branch.get("environment", {}) or {})
        plan.post_install = unique(as_list(branch.get("post_install", [])))
        plan.checks = list(as_list(branch.get("checks", []))) + self._common_checks(ident)
        plan.warnings = list(as_list(branch.get("warnings", [])))
        plan.fallback = dict(branch.get("fallback", {}) or {})
        plan.extra = {
            "pci": ident.device.key,
            "pci_description": ident.device.description,
            "in_database": ident.in_database,
            "identity_source": ident.source,
            "generation": ident.generation,
            "open_kernel_module": ident.open_kmod_ok,
            "handheld": info.is_handheld(),
            "secure_boot": info.secure_boot,
            "branch_priority": int(branch.get("priority", 0)),
        }
        self._apply_machine_rules(plan, ident, info)
        return plan

    def _select_branch(self, branches: list[dict[str, Any]], ident: GpuIdentity,
                       info: SystemInfo) -> dict[str, Any] | None:
        ranked: list[tuple[int, dict[str, Any]]] = []
        for branch in branches:
            ok, prio = self._branch_matches(branch, ident, info)
            if ok:
                ranked.append((prio + (5 if ident.in_database else 0), branch))
        if not ranked:
            return None
        ranked.sort(key=lambda t: -t[0])
        return ranked[0][1]

    def _branch_matches(self, branch: dict[str, Any], ident: GpuIdentity,
                        info: SystemInfo) -> tuple[bool, int]:
        req = branch.get("requires", {}) or {}
        prio = int(branch.get("priority", 0))
        gen = ident.generation
        if "generation_min" in req and gen < int(req["generation_min"]):
            return False, prio
        if "generation_max" in req and gen > int(req["generation_max"]):
            return False, prio
        if "vendors" in req and ident.vendor not in as_list(req["vendors"]):
            return False, prio
        if "families" in req and ident.family not in as_list(req["families"]):
            return False, prio
        if "not_families" in req and ident.family in as_list(req["not_families"]):
            return False, prio
        if req.get("open_kernel_module") is True and ident.open_kmod_ok is not True:
            return False, prio
        if req.get("open_kernel_module") is False and ident.open_kmod_ok is True and ident.in_database:
            return False, prio
        if "kmod" in req and ident.kmod != req["kmod"]:
            # The PCI database (generated from the kernel tree) decides which in-kernel
            # driver owns the device; branches may require that answer instead of
            # re-encoding device-id ranges here.
            return False, prio
        if req.get("secure_boot_off") is True and info.secure_boot:
            return False, prio
        if req.get("handheld") is True and not info.is_handheld():
            return False, prio
        if req.get("not_handheld") is True and info.is_handheld():
            return False, prio
        if req.get("laptop") is True and not info.is_laptop():
            return False, prio
        if req.get("in_database") is True and not ident.in_database:
            return False, prio
        if "kernel_min" in req and not _kernel_at_least(info.kernel, req["kernel_min"]):
            return False, prio
        if "pci_device_ids" in req:
            wanted = {norm_id(x) for x in as_list(req["pci_device_ids"])}
            if ident.device.device not in wanted:
                return False, prio
        for extra_key, weight in (req.get("boost", {}) or {}).items():
            if _truthy(getattr(info, extra_key, None) or info.extra.get(extra_key)):
                prio += int(weight)
        return True, prio + prio_bonus(branch, ident, info)

    def _cmdline_for_machine(self, info: SystemInfo, ident: GpuIdentity) -> list[str]:
        out: list[str] = []
        rules = self.general.get("cmdline_rules", []) or []
        for rule in rules:
            when = rule.get("when", {}) or {}
            if _machine_matches(when, info, ident):
                out.extend(as_list(rule.get("add", [])))
        return unique(out)

    def _apply_machine_rules(self, plan: DriverPlan, ident: GpuIdentity, info: SystemInfo) -> None:
        for rule in self.general.get("plan_rules", []) or []:
            if not _machine_matches(rule.get("when", {}) or {}, info, ident):
                continue
            for key in ("add_packages", "add_cmdline", "add_warning", "add_env", "enable", "mask"):
                values = as_list(rule.get(key, []))
                if not values:
                    continue
                if key == "add_packages":
                    plan.packages.extend(Package.parse(v) for v in values)
                elif key == "add_cmdline":
                    plan.cmdline = unique(plan.cmdline + [str(v) for v in values])
                elif key == "add_warning":
                    plan.warnings.extend(str(v) for v in values)
                elif key == "add_env":
                    for item in values:
                        if isinstance(item, str) and "=" in item:
                            k, _, v = item.partition("=")
                            plan.environment[k.strip()] = v.strip()
                elif key == "enable":
                    plan.services_enable = unique(plan.services_enable + [str(v) for v in values])
                elif key == "mask":
                    plan.services_mask = unique(plan.services_mask + [str(v) for v in values])
            if rule.get("id"):
                plan.extra.setdefault("matched_rules", []).append(rule["id"])
        if info.secure_boot and any("dkms" in p.name for p in plan.packages):
            plan.warnings.append(
                "Secure Boot is on and this stack uses DKMS modules: enroll the signing key "
                "with `hedron-drivers enroll-mok` (reboot once to confirm) or disable Secure Boot.")
        if ident.vendor == "nvidia" and ident.generation and ident.generation < 19 and not ident.in_database:
            plan.warnings.append(
                "this GPU predates the NVIDIA open kernel module; the legacy proprietary branch "
                "is used, which stops at the last driver series that supported it.")
        if ident.vendor == "amd" and ident.generation and ident.generation < 9:
            plan.warnings.append(
                "very old AMD GPU (pre-GCN): the radeon KMS driver + llvmpipe fallback are used; "
                "expect no Vulkan.")

    def _unknown_plan(self, info: SystemInfo, ident: GpuIdentity | None = None) -> DriverPlan:
        plan = DriverPlan(
            vendor=(ident.vendor if ident else "generic"),
            family=(ident.family if ident else "unknown"),
            branch="fallback",
            title="Generic DRM/KMS (no vendor stack selected)",
            kmod=(ident.kmod if ident else "simpledrm"),
            confidence=0.2,
        )
        for spec in as_list(self.general.get("fallback_packages", [])):
            plan.packages.append(Package.parse(spec))
        plan.warnings.append("No supported discrete GPU was detected; keeping the in-kernel "
                             "KMS driver and Mesa. Everything works, but nothing is tuned.")
        plan.initcpio_hooks = unique(as_list(self.general.get("initcpio_hooks", [])))
        plan.checks = [{"kind": "file", "path": "/dev/dri/renderD128", "expect": "exists",
                        "why": "a render node must exist even in fallback mode"}]
        return plan

    def _common_checks(self, ident: GpuIdentity) -> list[dict[str, Any]]:
        checks = [
            {"kind": "module", "name": ident.kmod, "expect": "loaded",
             "why": "the in-kernel driver must own the device"},
            {"kind": "file", "path": "/dev/dri/renderD128", "expect": "exists",
             "why": "DRI render node present"},
            {"kind": "command", "argv": ["glxinfo", "--brief"], "expect_contains": "",
             "optional": True, "why": "GLX renderer sanity"},
        ]
        return checks


def plan_branch_name(branch: dict[str, Any]) -> str:
    return str(branch.get("name") or branch.get("id") or "default")


def prio_bonus(branch: dict[str, Any], ident: GpuIdentity, info: SystemInfo) -> int:
    """Small deterministic nudges so the *nicest* matching branch wins ties."""
    bonus = 0
    name = plan_branch_name(branch).lower()
    if "open" in name and ident.open_kmod_ok is True:
        bonus += 20  # the open module is what we want on Turing+
    if "proprietary" in name or name.startswith("nvidia-"):
        bonus -= 1 if ident.open_kmod_ok is True else 0
    if "legacy" in name:
        bonus -= 15
    if info.is_handheld() and ("laptop" in name or "mobile" in name):
        bonus += 6
    return bonus


def _render_modprobe(branch: dict[str, Any], ident: GpuIdentity) -> dict[str, str]:
    mp = branch.get("modprobe", {}) or {}
    if not mp:
        return {}
    filename = mp.get("filename") or "99-hedron-%s.conf" % ident.vendor
    header = "# HedronOS — written by the driver engine (%s / %s). Edit with care.\n" % (
        ident.vendor, ident.family)
    lines = [header]
    for item in as_list(mp.get("lines", [])):
        lines.append(str(item) + "\n")
    for section, values in (mp.get("options", {}) or {}).items():
        if isinstance(values, dict):
            for opt, val in values.items():
                lines.append(f"options {section} {opt}={val}\n")
        else:
            lines.append(f"options {section} {values}\n")
    for black in as_list(mp.get("blacklist", [])):
        lines.append(f"blacklist {black}\n")
    return {filename: "".join(lines)}


def _machine_matches(when: dict[str, Any], info: SystemInfo, ident: GpuIdentity | None) -> bool:
    if not when:
        return True
    checks: list[bool] = []
    for key, want in when.items():
        if key == "vendors":
            checks.append(bool(ident) and ident.vendor in as_list(want))
        elif key == "generation_min":
            checks.append(bool(ident) and ident.generation >= int(want))
        elif key == "generation_max":
            checks.append(bool(ident) and ident.generation <= int(want))
        elif key == "family_prefix":
            checks.append(bool(ident) and ident.family.lower().startswith(str(want).lower()))
        elif key == "handheld":
            checks.append(info.is_handheld() == _truthy(want))
        elif key == "laptop":
            checks.append(info.is_laptop() == _truthy(want))
        elif key == "hybrid":
            checks.append(info.hybrid == _truthy(want))
        elif key == "secure_boot":
            checks.append(info.secure_boot == _truthy(want))
        elif key == "ram_max_mib":
            checks.append(info.memory_mib and info.memory_mib <= int(want))
        elif key == "ram_min_mib":
            checks.append(info.memory_mib >= int(want))
        elif key == "arch":
            checks.append(info.extra.get("arch", "x86_64") in as_list(want))
        elif key == "always":
            checks.append(_truthy(want))
        else:  # unknown predicate: fail closed rather than silently applying
            checks.append(False)
    return all(checks) if checks else True


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


def _kernel_at_least(release: str, want: str) -> bool:
    def parts(text: str) -> tuple[int, ...]:
        out = []
        for chunk in str(text or "0").split("."):
            digits = ""
            for ch in chunk:
                if ch.isdigit():
                    digits += ch
                else:
                    break
            out.append(int(digits or 0))
        return tuple(out + [0] * (4 - len(out)))
    if not release:
        return True  # unknown host kernel → don't block a branch we cannot disprove
    return parts(release) >= parts(want)


def _dedupe_packages(pkgs: list[Package]) -> list[Package]:
    best: dict[str, Package] = {}
    order: list[str] = []
    rank = {"required": 0, "recommended": 1, "optional": 2}
    for pkg in pkgs:
        if pkg.name not in best:
            best[pkg.name] = pkg
            order.append(pkg.name)
        elif rank.get(pkg.tier, 1) < rank.get(best[pkg.name].tier, 1):
            best[pkg.name] = pkg
    return [best[name] for name in order]


# --------------------------------------------------------------------- actions
def install_actions(plan: DriverPlan, *, root: str = "", dry_run: bool = False) -> list[dict[str, Any]]:
    """Turn a plan into an ordered, idempotent action list the installer executes.

    Actions are data (not code), which lets the installer journal them, the verifier
    check them, and `hedron-drivers --plan-only` print them without root.
    """
    actions: list[dict[str, Any]] = []
    for name, content in sorted(plan.modprobe_files.items()):
        actions.append({"kind": "write-file", "path": f"{root}/etc/modprobe.d/{name}",
                        "content": content, "mode": 0o644, "why": "module options for " + plan.vendor})
    for name, content in sorted(plan.files.items()):
        actions.append({"kind": "write-file", "path": f"{root}{name}", "content": content,
                        "mode": 0o644, "why": "driver stack drop-in"})
    required = plan.package_names(tiers=["required", "recommended"])
    if required:
        actions.append({"kind": "install-packages", "packages": required,
                        "why": "vendor graphics stack", "offline_ok": True})
    if plan.cmdline:
        actions.append({"kind": "kernel-cmdline", "add": list(plan.cmdline),
                        "drop": as_list(plan.extra.get("cmdline_drop", [])),
                        "why": "parameters this GPU needs"})
    if plan.initcpio_modules or plan.initcpio_hooks:
        actions.append({"kind": "set-env", "path": f"{root}/etc/mkinitcpio.conf.d/hedron-drivers.conf",
                        "values": {"MODULES": plan.initcpio_modules, "HOOKS_APPEND": plan.initcpio_hooks},
                        "why": "KMS must be up before the splash"})
    actions.append({"kind": "reload-initramfs", "preset": "linux-hedron", "why": "apply MODULES change"})
    for svc in plan.services_enable:
        actions.append({"kind": "enable-service", "unit": svc, "why": "vendor helper"})
    for svc in plan.services_mask:
        actions.append({"kind": "enable-service", "unit": svc, "state": "masked", "why": "known-bad helper"})
    for key, value in sorted(plan.environment.items()):
        actions.append({"kind": "set-env", "path": f"{root}/etc/environment.d/60-hedron-graphics.conf",
                        "values": {key: value}, "why": "session environment"})
    for hook in plan.post_install:
        actions.append({"kind": "run", "argv": hook, "why": "package post-install hook",
                        "allow_failure": True})
    if not dry_run:
        actions.append({"kind": "verify", "checks": list(plan.checks), "why": "prove it works"})
    order = {k: i for i, k in enumerate(ACTION_ORDER)}
    actions.sort(key=lambda a: order.get(a["kind"], 99))
    return actions


def plan_all_families(db: GpuDatabase, rules: dict[str, Any]) -> list[dict[str, Any]]:
    """Pre-bake one plan per (vendor, family) so the ISO covers machines it has never seen.

    Used by the build to decide exactly which driver packages must be cached on the
    image, and by `hedron-drivers --explain` to answer "what would you do for my GPU".
    """
    synthetic = SystemInfo(extra={"arch": "x86_64"})
    engine = DriverEngine(db, rules)
    plans: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for row in db.families():
        vendor = row["vendor"]
        if vendor not in engine.vendors:
            continue
        for device_id in _sample_ids_for_family(db, vendor, row["family"]):
            dev = PciDevice(vendor=_vendor_pci_id(vendor), device=device_id, class_code=0x030000)
            ident = db.lookup(dev)
            if not ident.family:
                ident.family = row["family"]
            plan = engine._plan_for_device(ident, synthetic)  # noqa: SLF001 - internal API by design
            key = (plan.vendor, plan.family, plan.branch)
            if key in seen:
                continue
            seen.add(key)
            entry = plan.as_dict()
            entry["sample_pci"] = ["%s:%s" % (hexnum(dev.vendor), hexnum(dev.device))]
            plans.append(entry)
    plans.sort(key=lambda p: (str(p.get("vendor")), str(p.get("family")), str(p.get("branch"))))
    return plans


def hexnum(value: int) -> str:
    return "0x%04x" % value


def _vendor_pci_id(vendor: str) -> int:
    return {"nvidia": 0x10DE, "amd": 0x1002, "intel": 0x8086}.get(vendor, 0x10DE)


def _sample_ids_for_family(db: GpuDatabase, vendor: str, family: str) -> list[int]:
    """Pick one or two real device ids belonging to a family from the database."""
    out: list[int] = []
    for row in db.all_rows():
        fam = row.get("family") or row.get("chip") or row.get("platform") or ""
        if fam != family:
            continue
        for fld in ("device_ids", "ids", "id"):
            value = row.get(fld)
            if value is None:
                continue
            for item in value if isinstance(value, list) else [value]:
                try:
                    out.append(norm_id(item))
                except ValueError:
                    continue
        if out:
            break
    return unique(out) or [0x0000]
