"""The HedronOS app index: format, signing and verification.

One index, four backends. The GUI, `hstore`, the installer's chooser and `hedron-doctor` all
read the same document, so "what you saw in the installer" and "what you can install now"
cannot drift apart — which is exactly how most distros' "software centre vs real repo" stories
diverge.

Format (`hedron-store-index/1`), a JSON object:

  { "format": "hedron-store-index/1", "generated": <unix>, "version": "2026.11.07",
    "mirror": "https://…", "entries": [ {…}, … ], "categories": [...], "bundles": {...} }
  entry = {id, name, summary, category, source(pacman|flatpak|appimage|hedron|aur),
           provider(package id / flatpak app id / URL), version, size_mib, tiers(verified|
           community|unsupported), flags[], install(app-id extra), screenshot, url}

Integrity: the JSON is canonicalised (sorted keys, no whitespace variance) and the digest is
detached-signed with Ed25519 (`openssl pkeyutl`), stored next to it as `<index>.sig`. We
verify with the pubkey shipped in `hedronos-release` — so a mirrored or MITM'd index is
rejected rather than half-trusted. No new crypto dependency, no bespoke key format: the same
primitive the ISO signing step uses.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Iterable

from .util import sha256_bytes, sha256_text

FORMAT = "hedron-store-index/1"
SOURCES = ("pacman", "flatpak", "appimage", "hedron", "aur")
TIERS = ("verified", "community", "unsupported")
DEFAULT_PUBKEY = "/etc/hedron/store.pub"


class IndexError_(Exception):
    """Raised for a malformed or untrusted index (named to avoid clashing with builtins)."""


def canonical(data: Any) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


@dataclass
class Entry:
    id: str
    name: str
    summary: str = ""
    category: str = "other"
    source: str = "pacman"
    provider: str = ""
    version: str = ""
    size_mib: float = 0.0
    tier: str = "community"
    flags: list[str] = field(default_factory=list)
    install: list[str] = field(default_factory=list)
    url: str = ""
    screenshot: str = ""
    locked: bool = False
    selected: bool = False

    @staticmethod
    def from_dict(doc: dict[str, Any]) -> "Entry":
        known = {f for f in Entry.__dataclass_fields__}  # type: ignore[attr-defined]
        return Entry(**{k: v for k, v in doc.items() if k in known})

    def as_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in ("", [], 0.0, False) or k == "id"}

    @property
    def key(self) -> str:
        return "%s:%s" % (self.source, self.provider or self.id)


@dataclass
class Index:
    entries: list[Entry]
    meta: dict[str, Any] = field(default_factory=dict)
    categories: list[dict[str, Any]] = field(default_factory=list)
    path: str | None = None
    signature: str | None = None
    verified: bool | None = None

    def by_id(self, app_id: str) -> Entry | None:
        for entry in self.entries:
            if entry.id == app_id:
                return entry
        return None

    def by_source(self, source: str) -> list[Entry]:
        return [e for e in self.entries if e.source == source]

    def search(self, text: str, *, category: str | None = None, source: str | None = None,
               tier: str | None = None) -> list[Entry]:
        text = text.lower().strip()
        out = []
        for entry in self.entries:
            if category and entry.category != category:
                continue
            if source and entry.source != source:
                continue
            if tier and entry.tier != tier:
                continue
            if not text:
                out.append(entry)
                continue
            haystack = " ".join((entry.id, entry.name, entry.summary, entry.category,
                                  entry.provider)).lower()
            if all(word in haystack for word in text.split()):
                out.append(entry)
        return sorted(out, key=lambda e: (e.tier != "verified", e.category, e.name.lower()))

    def validate(self) -> list[str]:
        problems: list[str] = []
        if self.meta.get("format") != FORMAT:
            problems.append("index format is %r, expected %r" % (self.meta.get("format"), FORMAT))
        seen: set[str] = set()
        for entry in self.entries:
            if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", entry.id):
                problems.append("entry id %r is not kebab/lowest" % entry.id)
            if entry.id in seen:
                problems.append("duplicate entry id %r" % entry.id)
            seen.add(entry.id)
            if entry.source not in SOURCES:
                problems.append("%s: source %r not in %s" % (entry.id, entry.source, SOURCES))
            if entry.tier not in TIERS:
                problems.append("%s: tier %r not in %s" % (entry.id, entry.tier, TIERS))
            if not entry.provider:
                problems.append("%s: no provider, so nothing can be installed" % entry.id)
            if not entry.summary:
                problems.append("%s: empty summary (the chooser shows this line)" % entry.id)
            if entry.source == "flatpak" and not re.match(r"^[A-Za-z0-9_.-]+$", entry.provider):
                problems.append("%s: flatpak provider %r is not an app id" % (entry.id, entry.provider))
        return problems

    def stats(self) -> dict[str, Any]:
        by_source: dict[str, int] = {}
        by_tier: dict[str, int] = {}
        by_cat: dict[str, int] = {}
        size = 0.0
        for entry in self.entries:
            by_source[entry.source] = by_source.get(entry.source, 0) + 1
            by_tier[entry.tier] = by_tier.get(entry.tier, 0) + 1
            by_cat[entry.category] = by_cat.get(entry.category, 0) + 1
            size += entry.size_mib
        return {"entries": len(self.entries), "by_source": by_source, "by_tier": by_tier,
                "by_category": by_cat, "total_mib": round(size, 1),
                "generated": self.meta.get("generated"), "version": self.meta.get("version"),
                "verified": self.verified}


# ------------------------------------------------------------------- digest/sig
def digest(data: dict[str, Any]) -> str:
    return sha256_bytes(canonical(data))


def sign(index_path: str, key: str, *, dry_run: bool = False) -> str | None:
    """Detached Ed25519 signature over the canonical JSON (see `canonical`)."""
    if not os.path.isfile(key):
        raise IndexError_("no signing key at %s (create one: hedron-store-keygen)" % key)
    data = json.load(open(index_path, encoding="utf-8"))
    sig = index_path + ".sig"
    if dry_run:
        return sig
    with tempfile.NamedTemporaryFile("wb", suffix=".bin", delete=False) as fh:
        fh.write(canonical(data))
        payload = fh.name
    try:
        proc = subprocess.run(["openssl", "pkeyutl", "-sign", "-inkey", key, "-rawin", "-in", payload],
                              capture_output=True)
        if proc.returncode != 0:
            raise IndexError_("openssl sign failed: %s" % proc.stderr.decode()[:200])
        with open(sig, "w", encoding="utf-8") as fh:
            fh.write(proc.stdout.hex() + "\n")
    finally:
        os.unlink(payload)
    return sig


def verify(index_path: str, pubkey: str = DEFAULT_PUBKEY, *, required: bool | None = None) -> tuple[bool, str]:
    """Return (ok, explanation). A missing signature is a *finding*, never silently accepted."""
    required = os.environ.get("HEDRON_STORE_REQUIRE_SIGNATURE", "") == "1" if required is None else required
    data = json.load(open(index_path, encoding="utf-8"))
    sig_path = index_path + ".sig"
    if not os.path.isfile(sig_path):
        if required:
            return False, "no %s and signature verification is required" % os.path.basename(sig_path)
        return True, "unsigned (accepted because HEDRON_STORE_REQUIRE_SIGNATURE is not set)"
    if not os.path.isfile(pubkey):
        return (False, "signature present but %s is missing — cannot trust this index" % pubkey)
    try:
        sig = bytes.fromhex(open(sig_path, encoding="utf-8").read().strip())
    except ValueError:
        return False, "signature file is not hex"
    with tempfile.NamedTemporaryFile("wb", suffix=".bin", delete=False) as fh:
        fh.write(canonical(data))
        payload = fh.name
    try:
        proc = subprocess.run(["openssl", "pkeyutl", "-verify", "-pubin", "-inkey", pubkey,
                               "-rawin", "-in", payload, "-sigfile", "/dev/stdin"],
                              input=sig, capture_output=True)
    finally:
        os.unlink(payload)
    if proc.returncode != 0:
        return False, "signature does not verify: %s" % proc.stderr.decode()[:180]
    return True, "signature verified against %s" % os.path.basename(pubkey)


# ------------------------------------------------------------------ load / build
def load(path: str, *, verify_signature: bool = True) -> Index:
    if not os.path.isfile(path):
        raise IndexError_("index not found: %s (run `hstore update`)" % path)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except json.JSONDecodeError as exc:
        raise IndexError_("%s is not valid JSON: %s" % (path, exc)) from exc
    if data.get("format") != FORMAT:
        raise IndexError_("%s has format %r; this build understands %r" % (path, data.get("format"), FORMAT))
    entries = [Entry.from_dict(e) for e in data.get("entries", [])]
    idx = Index(entries=entries, meta={k: v for k, v in data.items() if k not in ("entries", "categories")},
                categories=data.get("categories", []), path=path)
    if verify_signature:
        ok, why = verify(path)
        idx.verified = ok
        idx.meta["verification"] = why
        if not ok and os.environ.get("HEDRON_STORE_REQUIRE_SIGNATURE", "") == "1":
            raise IndexError_(why)
    return idx


def build(entries: Iterable[Entry], *, version: str, mirror: str = "", categories: list[dict] | None = None,
          extra: dict | None = None) -> dict[str, Any]:
    doc: dict[str, Any] = {"format": FORMAT, "generated": int(time.time()), "version": version,
                           "mirror": mirror, "entries": [e.as_dict() for e in entries],
                           "categories": categories or []}
    if extra:
        doc.update(extra)
    return doc


def write(index: dict[str, Any], path: str, *, sign_key: str | None = None) -> str:
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(index, fh, indent=1, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)
    if sign_key:
        sign(path, sign_key)
    return path


def keygen(directory: str, *, force: bool = False) -> tuple[str, str]:
    """Create the Ed25519 keypair the release manager signs indexes with."""
    os.makedirs(directory, exist_ok=True)
    priv, pub = os.path.join(directory, "store-ed25519"), os.path.join(directory, "store.pub")
    if os.path.exists(priv) and not force:
        raise IndexError_("%s exists (use --force to overwrite: that invalidates every "
                          "previously signed index)" % priv)
    if not shutil.which("openssl"):
        raise IndexError_("openssl is required to generate keys")
    subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", priv], check=True)
    subprocess.run(["openssl", "pkey", "-pubout", "-in", priv, "-out", pub], check=True)
    os.chmod(priv, 0o600)
    return priv, pub


def entry_for(app: Any) -> Entry:
    """Map a catalogue App onto an installable index entry.

    One app can have several providers (pacman + flatpak + AUR); the store's precedence is
    hedron → flatpak → appimage → aur → pacman, because that is the order in which a wrong
    choice costs you something (a HedronOS package is curated, a raw AUR build is not).
    """
    if getattr(app, "hedron", ""):
        source, provider = "hedron", app.hedron
    elif getattr(app, "flatpak", ""):
        source, provider = "flatpak", app.flatpak
    elif getattr(app, "appimage", ""):
        source, provider = "appimage", app.appimage
    elif getattr(app, "aur", ""):
        source, provider = "aur", app.aur
    elif getattr(app, "pacman", []):
        source, provider = "pacman", list(app.pacman)[0]
    else:
        raise IndexError_("%s: no provider at all" % app.id)
    flags = list(getattr(app, "tags", []) or [])
    if getattr(app, "gaming", False):
        flags.append("gaming")
    if getattr(app, "sandbox", "none") not in ("", "none"):
        flags.append("sandbox=" + app.sandbox)
    return Entry(
        id=app.id, name=app.name, summary=app.summary, category=app.category,
        source=source, provider=provider,
        version="",                      # filled in at resolve time from the live repo
        size_mib=float(getattr(app, "size_mib", 0.0) or 0.0),
        tier=getattr(app, "tier", "community") or "community",
        flags=flags,
        install=list(getattr(app, "post_install", []) or []),
        url="",
        screenshot=getattr(app, "icon", "") or "",
        locked=bool(getattr(app, "locked", False)),
    )


def from_catalog(catalog: Any, *, version: str, mirror: str = "", bundles: bool = True,
                 profiles: dict[str, list[str]] | None = None) -> dict[str, Any]:
    """Derive the installable index from the same apps.toml the chooser shows.

    This is why the installer and the store cannot disagree: the index is generated from the
    catalogue, not maintained beside it. `profiles` marks which ids are pre-selected per
    profile, so the store can show "recommended for you" without a second data file.
    """
    entries = [entry_for(app) for app in catalog.apps.values() if not getattr(app, "hidden", False)]
    selected: dict[str, set[str]] = {}
    for profile in (profiles or {}):
        try:
            selected[profile] = set(catalog.default_selection(profile))
        except Exception:  # noqa: BLE001 - a broken profile list must not break the index
            selected[profile] = set()
    for entry in entries:
        for profile, ids in selected.items():
            if entry.id in ids:
                entry.flags.append("default:" + profile)
        if entry.locked:
            entry.flags.append("locked")
    categories = [{"id": c.get("id"), "name": c.get("name"), "icon": c.get("icon", "application-x-executable")}
                  for c in getattr(catalog, "categories", []) if isinstance(c, dict)]
    doc = build(entries, version=version, mirror=mirror, categories=categories)
    if bundles:
        doc["bundles"] = {name: list(ids) for name, ids in (getattr(catalog, "bundles", {}) or {}).items()}
    return doc


def sha(index_path: str) -> str:
    return sha256_text(open(index_path, encoding="utf-8").read())
