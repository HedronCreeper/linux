# SPDX-License-Identifier: MIT
"""The curated application catalog — one file, three users.

`data/apps.toml` is read by:
  * `hedron-firstsetup`  → the pre-install app picker (with `locked = true` items)
  * `hedron-store`        → the "Curated" tab of the store after install
  * `tools/hedronos-verify` → catalog validation (ids, providers, sizes, category refs)

An entry declares *how* it is provided (`pacman` packages, `flatpak` app ids, `appimage`
URLs, or a `hedron` package from this repo). Everything else — icons, descriptions,
defaults, post-install hooks — is data, so adding an app never means writing code.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .util import as_list, load_toml, unique

ID_RE = re.compile(r"^[a-z0-9][a-z0-9+._-]{0,62}$")
APP_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
TIERS = {"verified", "community", "unsupported"}
CATEGORIES: tuple[str, ...] = (
    "system", "gaming", "emulation", "streaming", "browsers", "communication", "office",
    "creative", "media", "development", "utilities", "security", "hardware", "network",
)


@dataclass
class App:
    id: str
    name: str
    category: str = "utilities"
    summary: str = ""
    description: str = ""
    pacman: list[str] = field(default_factory=list)
    flatpak: str = ""
    appimage: str = ""
    hedron: str = ""
    aur: str = ""
    tags: list[str] = field(default_factory=list)
    size_mib: float = 60.0
    tier: str = "community"
    profiles: list[str] = field(default_factory=list)
    locked: bool = False
    gaming: bool = False
    post_install: list[str] = field(default_factory=list)
    sandbox: str = "none"
    icon: str = ""
    notes: str = ""
    requires: list[str] = field(default_factory=list)
    conflicts: list[str] = field(default_factory=list)
    hidden: bool = False

    # ------------------------------------------------------------------ helpers
    @property
    def providers(self) -> list[str]:
        """Human-readable description of where this comes from."""
        out = []
        if self.pacman:
            out.append("pacman: " + " ".join(self.pacman))
        if self.flatpak:
            out.append("flatpak: " + self.flatpak)
        if self.appimage:
            out.append("appimage")
        if self.hedron:
            out.append("hedron: " + self.hedron)
        if self.aur:
            out.append("aur: " + self.aur)
        return out

    def package_names(self) -> list[str]:
        return list(self.pacman) + ([self.hedron] if self.hedron else [])

    def as_dict(self) -> dict[str, Any]:
        d = {k: getattr(self, k) for k in self.__dataclass_fields__}
        d["providers"] = self.providers
        return d

    @staticmethod
    def from_toml(row: dict[str, Any]) -> "App":
        known = set(App.__dataclass_fields__)
        kwargs: dict[str, Any] = {}
        for key, value in row.items():
            if key in known:
                kwargs[key] = value
        for list_field in ("pacman", "tags", "profiles", "post_install", "requires", "conflicts"):
            if list_field in kwargs:
                kwargs[list_field] = [str(v) for v in as_list(kwargs[list_field])]
        # A flatpak-only app is sandboxed by definition: make the data say so.
        if "sandbox" not in kwargs and kwargs.get("flatpak"):
            kwargs["sandbox"] = "flatpak"
        kwargs["size_mib"] = float(kwargs.get("size_mib", 60.0))
        for flag in ("locked", "gaming", "hidden"):
            kwargs[flag] = bool(kwargs.get(flag, False))
        return App(**kwargs)


class Catalog:
    def __init__(self, data: dict[str, Any]):
        self.raw = data
        self.meta = data.get("meta", {}) or {}
        # The catalogue may declare categories either as an array of tables
        # ([[category]] + id/name) or as a table of tables ([categories.gaming]). Both read the
        # same way so the installer, the store index and the verifier see one shape.
        cats = data.get("categories") or data.get("category") or []
        if isinstance(cats, dict):
            cats = [dict(v, id=k) for k, v in sorted(cats.items()) if isinstance(v, dict)]
        self.categories = cats
        self.bundles = data.get("bundles", {}) or {}
        self._category_ids = {c.get("id") for c in self.categories if isinstance(c, dict)}
        self.apps: dict[str, App] = {}
        for row in data.get("app", []):
            app = App.from_toml(row)
            self.apps[app.id] = app

    # -------------------------------------------------------------------- load
    @classmethod
    def load(cls, path: str) -> "Catalog":
        return cls(load_toml(path))

    # ------------------------------------------------------------------- query
    def visible(self) -> list[App]:
        return [a for a in sorted(self.apps.values(), key=lambda a: (a.category, a.name.lower()))
                if not a.hidden]

    def by_category(self, category: str) -> list[App]:
        return [a for a in sorted(self.apps.values(), key=lambda a: a.name.lower())
                if a.category == category and not a.hidden]

    def gaming(self) -> list[App]:
        return [a for a in self.visible() if a.gaming or a.category in ("gaming", "emulation", "streaming")]

    def locked_ids(self) -> list[str]:
        return sorted(a.id for a in self.apps.values() if a.locked and not a.hidden)

    def default_selection(self, profile: str) -> list[str]:
        """Which checkboxes are pre-ticked for a given build profile."""
        out = []
        for app in self.apps.values():
            if app.hidden:
                continue
            if app.locked or profile in app.profiles or ("*" in app.profiles):
                out.append(app.id)
        return sorted(out)

    def bundle(self, name: str) -> list[str]:
        """Named selection presets shown as one-click chips in the installer."""
        entry = self.bundles.get(name)
        if not entry:
            return []
        ids = [str(x) for x in as_list(entry.get("apps", []))]
        for cat in as_list(entry.get("categories", [])):
            ids += [a.id for a in self.apps.values() if a.category == cat and not a.hidden and a.tier != "unsupported"]
        return unique(ids)

    def expand(self, app_ids: Iterable[str]) -> dict[str, list[str]]:
        """Turn selected app ids into what must be installed, per provider."""
        repos: list[str] = []
        flatpaks: list[str] = []
        appimages: list[str] = []
        hedron: list[str] = []
        missing: list[str] = []
        unknown: list[str] = []
        for app_id in app_ids:
            app = self.apps.get(app_id)
            if app is None:
                unknown.append(app_id)
                continue
            repos.extend(app.pacman)
            if app.flatpak:
                flatpaks.append(app.flatpak)
            if app.appimage:
                appimages.append(app.appimage)
            if app.hedron:
                hedron.append(app.hedron)
            if not (app.pacman or app.flatpak or app.appimage or app.hedron or app.aur):
                missing.append(app.id)
            for req in app.requires:
                if req in self.apps:
                    repos.extend(self.apps[req].pacman)
                    if self.apps[req].flatpak:
                        flatpaks.append(self.apps[req].flatpak)
                else:
                    unknown.append("%s (required by %s)" % (req, app.id))
        return {
            "pacman": unique(repos),
            "flatpak": unique(flatpaks),
            "appimage": unique(appimages),
            "hedron": unique(hedron),
            "missing_provider": missing,
            "unknown_refs": unknown,
        }

    def post_install_commands(self, app_ids: Iterable[str]) -> list[tuple[str, str]]:
        """(app id, command) pairs the installer runs after packages land."""
        out: list[tuple[str, str]] = []
        for app_id in app_ids:
            app = self.apps.get(app_id)
            if not app:
                continue
            for cmd in app.post_install:
                out.append((app.id, cmd))
        return out

    def size_estimate(self, app_ids: Iterable[str]) -> float:
        total = 0.0
        for app_id in app_ids:
            app = self.apps.get(app_id)
            if app:
                total += app.size_mib
        return total

    # ---------------------------------------------------------------- validation
    def validate(self) -> list[str]:
        """Return a list of human-readable problems; empty means the catalog is good."""
        problems: list[str] = []
        ids_seen: dict[str, str] = {}
        if not self.categories:
            problems.append("no [[category]] entries declared: the installer's category browser "
                            "and hstore's --category filter would both be empty")
        declared = {c.get("id") for c in self.categories if isinstance(c, dict)}
        for row in self.categories:
            if not isinstance(row, dict) or not row.get("id"):
                problems.append("category entry without an id: %r" % (row,))
            elif not str(row.get("name", "")).strip():
                problems.append("category %r has no display name" % row.get("id"))
        for orphan in sorted(declared - {c for c in declared if c}):
            pass
        for app in self.apps.values():
            if not ID_RE.match(app.id):
                problems.append("app id %r is not kebab-case-safe" % app.id)
            if app.id in ids_seen:
                problems.append("duplicate app id %r (%s vs %s)" % (app.id, ids_seen[app.id], app.name))
            ids_seen[app.id] = app.name
            if not app.name.strip():
                problems.append("%s: empty display name" % app.id)
            if not app.summary.strip():
                problems.append("%s: empty summary (installer shows nothing to pick on)" % app.id)
            if len(app.summary) > 120:
                problems.append("%s: summary is %d chars, keep it under 120 for the picker" % (app.id, len(app.summary)))
            if app.category not in CATEGORIES:
                problems.append("%s: unknown category %r" % (app.id, app.category))
            if self._category_ids and app.category not in self._category_ids:
                problems.append("%s: category %r has no entry in [[category]]" % (app.id, app.category))
            if app.tier not in TIERS:
                problems.append("%s: tier %r not in %s" % (app.id, app.tier, sorted(TIERS)))
            if not (app.pacman or app.flatpak or app.appimage or app.hedron or app.aur):
                problems.append("%s: no provider (needs pacman/flatpak/appimage/hedron/aur)" % app.id)
            if app.flatpak and not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]*\.[A-Za-z0-9_.-]+$", app.flatpak):
                problems.append("%s: flatpak app id %r is not reverse-DNS" % (app.id, app.flatpak))
            if app.size_mib <= 0:
                problems.append("%s: size_mib must be positive" % app.id)
            if app.sandbox not in {"none", "auto", "flatpak", "bubblewrap", "firejail", "apparmor"}:
                problems.append("%s: unknown sandbox mode %r" % (app.id, app.sandbox))
            for ref in app.conflicts:
                if ref not in self.apps:
                    problems.append("%s: conflicts with unknown app %r" % (app.id, ref))
            for ref in app.requires:
                if ref not in self.apps:
                    problems.append("%s: requires unknown app %r" % (app.id, ref))
            for prof in app.profiles:
                if prof != "*" and prof not in {"gamerte", "desktop", "handheld", "studio", "core"}:
                    problems.append("%s: unknown profile %r in profiles" % (app.id, prof))
        for name, entry in (self.bundles or {}).items():
            for app_id in as_list(entry.get("apps", [])):
                if app_id not in self.apps:
                    problems.append("bundle %s references unknown app %r" % (name, app_id))
        if not any(a.locked for a in self.apps.values()):
            problems.append("no app is locked: the gaming profile must pin Steam")
        if "steam" not in self.apps:
            problems.append("catalog must contain 'steam' (the profile promises it)")
        return problems
