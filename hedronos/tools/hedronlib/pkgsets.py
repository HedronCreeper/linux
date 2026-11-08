# SPDX-License-Identifier: MIT
"""Profile → concrete package list.

The resolver is deliberately boring and total:

  groups from the profile  +  variant additions  -  profile excludes
  +  hedron packages (built from packages/*)      -  conflicting packages
  =  a sorted, deduplicated install plan, with a provenance note for every package.

That provenance record is what makes `hedron-doctor` and the verifier able to answer
"why is this on my system?" later, which is a question no other distro can answer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .util import as_list, unique

SOURCE_REPO = "repo"
SOURCE_HEDRON = "hedron"
SOURCE_AUR = "aur"
SOURCE_FLATPAK = "flatpak"
VALID_SOURCES = (SOURCE_REPO, SOURCE_HEDRON, SOURCE_AUR, SOURCE_FLATPAK)


@dataclass
class Resolved:
    name: str
    source: str = SOURCE_REPO
    reason: str = ""
    group: str = ""
    tier: str = "required"
    estimate_mib: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "source": self.source, "reason": self.reason,
                "group": self.group, "tier": self.tier, "estimate_mib": round(self.estimate_mib, 1)}


@dataclass
class Resolution:
    profile: str
    variant: str
    arch: str
    packages: list[Resolved] = field(default_factory=list)
    flatpaks: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    conflicts_applied: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    size_mib: float = 0.0

    def names(self) -> list[str]:
        return [p.name for p in self.packages]

    def select(self, source: str | None = None, tier: str | None = None) -> list[str]:
        out = []
        for p in self.packages:
            if source and p.source != source:
                continue
            if tier and p.tier != tier:
                continue
            out.append(p.name)
        return out

    def as_dict(self) -> dict[str, Any]:
        return {
            "format": "hedronos-pkgset/1",
            "profile": self.profile,
            "variant": self.variant,
            "arch": self.arch,
            "packages": [p.as_dict() for p in self.packages],
            "flatpaks": list(self.flatpaks),
            "notes": list(self.notes),
            "conflicts_applied": list(self.conflicts_applied),
            "errors": list(self.errors),
            "size_mib": round(self.size_mib, 1),
            "count": len(self.packages),
        }


class Resolver:
    def __init__(self, sets: dict[str, Any], hedron_packages: dict[str, Any] | None = None):
        self.sets = sets
        self.hedron = hedron_packages or {}
        self.meta = sets.get("meta", {}) or {}
        self.groups = sets.get("groups", {}) or {}
        self.profiles = sets.get("profiles", {}) or {}
        self.hedron_groups = sets.get("hedron", {}) or {}
        self.conflicts = sets.get("conflicts", []) or []
        self.excluded = sets.get("excluded_by_default", {}) or {}

    # ------------------------------------------------------------------ public
    def profile_names(self) -> list[str]:
        return sorted(self.profiles)

    def group_names(self) -> list[str]:
        return sorted(self.groups)

    def resolve(self, profile: str, variant: str = "dvd", arch: str = "x86_64",
                *, include_default_apps: bool = True) -> Resolution:
        res = Resolution(profile=profile, variant=variant, arch=arch)
        spec = self.profiles.get(profile)
        if spec is None:
            res.errors.append("unknown profile '%s' (known: %s)" % (profile, ", ".join(self.profile_names())))
            return res

        picked: dict[str, Resolved] = {}

        def add(name: str, source: str, reason: str, group: str, estimate: float = 0.0,
                tier: str = "required") -> None:
            name = name.strip()
            if not name:
                return
            if name in picked:
                existing = picked[name]
                if existing.source == SOURCE_REPO and source == SOURCE_HEDRON:
                    existing.source = SOURCE_HEDRON
                    existing.reason += "; overridden by the HedronOS build"
                return
            picked[name] = Resolved(name, source, reason, group, tier, estimate)

        for name in as_list(self.meta.get("mandatory", [])):
            add(name, SOURCE_REPO, "mandatory in every HedronOS profile", "mandatory", 2.0)

        for group in as_list(spec.get("groups", [])):
            g = self.groups.get(group)
            if g is None:
                res.errors.append("profile %s references unknown group '%s'" % (profile, group))
                continue
            per_pkg = float(g.get("estimate_mib", 0) or 0) / max(1, len(as_list(g.get("packages", []))))
            for name in as_list(g.get("packages", [])):
                add(name, SOURCE_REPO, g.get("note", "").strip() or ("group " + group), group, per_pkg)
            for name in as_list(g.get("hedron", [])):
                add(name, SOURCE_HEDRON, "Hedron package from group " + group, group, 1.0)
            for name in as_list(g.get("optional", [])):
                add(name, SOURCE_REPO, "optional group member", group, per_pkg, tier="optional")
            for name in as_list(g.get("flatpak", [])):
                if name not in res.flatpaks:
                    res.flatpaks.append(name)

        for name in as_list(self.hedron_groups.get("core", [])):
            add(name, SOURCE_HEDRON, "HedronOS core tooling", "hedron-core", 1.0)
        extra_hedron = {"gamerte": ["gaming"], "handheld": ["gaming", "handheld"],
                        "desktop": [], "studio": [], "core": []}.get(profile, [])
        for gname in extra_hedron:
            for name in as_list(self.hedron_groups.get(gname, [])):
                add(name, SOURCE_HEDRON, "HedronOS %s components" % gname, "hedron-" + gname, 1.0)

        vconf = self.sets.get("variant", {}) or {}
        v = vconf.get(variant, {}) or {}
        if variant != "dvd" and not v:
            res.errors.append("unknown variant '%s' (known: %s)" % (variant, ", ".join(vconf)))
        for name in as_list(v.get("add_packages", [])):
            add(name, SOURCE_REPO, "variant " + variant, "variant", 40.0)
        for group in as_list(v.get("add_groups", [])):
            g = self.groups.get(group)
            if g is None:
                res.errors.append("variant %s references unknown group '%s'" % (variant, group))
                continue
            for name in as_list(g.get("packages", [])):
                add(name, SOURCE_REPO, "variant group " + group, group, 5.0)

        if include_default_apps:
            for name in as_list(spec.get("default_apps", [])):
                entry = self._app_source(name)
                if entry is None:
                    add(name, SOURCE_AUR, "profile default app (unlisted: resolved at runtime)", "apps", 20.0)
                else:
                    source, reason = entry
                    add(name, source, reason, "apps", 25.0)

        # ---- subtractive rules
        for name in as_list(spec.get("exclude", [])):
            if picked.pop(name.strip(), None) is not None:
                res.notes.append("profile excluded %s" % name)
        for name in as_list(self.excluded.get("packages", [])):
            if picked.pop(name.strip(), None) is not None:
                res.notes.append("excluded distro-wide: %s (%s)" % (name, self.excluded.get("note", "")))
        profile_conf = as_list(spec.get("pkg_exclude", []))
        for name in profile_conf:
            if picked.pop(name.strip(), None) is not None:
                res.notes.append("profile.conf excluded %s" % name)

        # ---- conflicts: keep the first, drop the second, and say so
        for conflict in self.conflicts:
            a, b = conflict.get("a"), conflict.get("b")
            if a in picked and b in picked:
                dropped = b
                picked.pop(dropped, None)
                res.conflicts_applied.append("%s wins over %s: %s" % (a, dropped, conflict.get("why", "")))

        res.packages = [picked[k] for k in sorted(picked)]
        res.size_mib = sum(p.estimate_mib for p in res.packages)
        res.notes.append("resolved %d packages, %.1f GiB estimated" % (len(res.packages), res.size_mib / 1024))
        if arch not in ("x86_64", "aarch64"):
            res.errors.append("arch %s has no package sets in this data file" % arch)
        for p in res.packages:
            if p.source == SOURCE_HEDRON and p.name not in self.hedron and self.hedron:
                res.errors.append("%s is marked as a Hedron package but packages/%s is missing" % (p.name, p.name))
        return res

    def _app_source(self, name: str) -> tuple[str, str] | None:
        for group, g in self.groups.items():
            if name in as_list(g.get("packages", [])):
                return SOURCE_REPO, "default app, provided by group " + group
            if name in as_list(g.get("flatpak", [])):
                return SOURCE_FLATPAK, "default app, flatpak only"
            if name in as_list(g.get("hedron", [])):
                return SOURCE_HEDRON, "default app, HedronOS package"
        return None

    # ----------------------------------------------------------------- reports
    def size_report(self, res: Resolution) -> list[str]:
        by_group: dict[str, list[Resolved]] = {}
        for p in res.packages:
            by_group.setdefault(p.group or "ungrouped", []).append(p)
        out = []
        for group in sorted(by_group, key=lambda g: -sum(p.estimate_mib for p in by_group[g])):
            items = by_group[group]
            out.append("%-16s %4d pkgs  %8.1f MiB" % (group, len(items), sum(i.estimate_mib for i in items)))
        out.append("%-16s %4d pkgs  %8.1f MiB" % ("TOTAL", len(res.packages), res.size_mib))
        return out


def load_sets(data_dir: str, packages_dir: str | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    import os

    from .util import load_toml
    sets = load_toml(os.path.join(data_dir, "package-sets.toml"))
    hedron: dict[str, Any] = {}
    if packages_dir and os.path.isdir(packages_dir):
        for name in sorted(os.listdir(packages_dir)):
            manifest = os.path.join(packages_dir, name, "hedronpkg.toml")
            if os.path.isfile(manifest):
                hedron[name] = load_toml(manifest)
    return sets, hedron


def dedupe_sources(items: Iterable[Resolved]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {s: [] for s in VALID_SOURCES}
    for item in items:
        out.setdefault(item.source, []).append(item.name)
    for values in out.values():
        values.sort()
    return out


def merge_extra(res: Resolution, extra: Iterable[str], source: str = SOURCE_REPO,
                reason: str = "user selection") -> None:
    seen = {p.name for p in res.packages}
    for name in unique(extra):
        name = str(name).strip()
        if not name or name in seen:
            continue
        seen.add(name)
        res.packages.append(Resolved(name, source, reason, "extra", 20.0))
    res.packages.sort(key=lambda p: p.name)
