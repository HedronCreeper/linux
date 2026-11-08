# SPDX-License-Identifier: MIT
"""HedronOS self-verification — the checks that run before anything is pushed.

This is the answer to "make sure the whole system works". A distro cannot be *run* in a
container that has no pacman, no xorriso and no GPU, so instead we verify the properties that
actually predict whether it will work on a real machine, and we verify them through the same
code paths the build uses (parse the manifests, load the catalogue, resolve every profile,
plan every GPU family, run every shipped CLI with --help) rather than with `path.exists()`
assertions, which pass on empty files.

Every check returns a `Check` (see util.Check). Severity `warn` never fails the gate — use it
for judgement calls; `error` means a user would hit a broken install.
"""

from __future__ import annotations

import ast
import glob
import json
import os
import py_compile
import re
import subprocess
import sys
import tomllib
import tempfile
from typing import Any, Callable, Iterable

from .util import Check, load_json, load_toml, run, sha256_file, sha256_text

# py_compile refuses to write its artefact to /dev/null, and we do not want stray .pyc files
# in the tree, so the syntax check compiles into one shared temporary directory.
_TMP_CT = tempfile.TemporaryDirectory(prefix="hedronos-verify-")

HEDRON_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
VERSIONS = ("error", "warn", "info")


class Context:
    """Everything a check needs to know about where things live."""

    def __init__(self, root: str = HEDRON_ROOT, work: str | None = None, kernel: str | None = None):
        self.root = root
        self.packages = os.path.join(root, "packages")
        self.tools = os.path.join(root, "tools")
        self.data = os.path.join(root, "data")
        self.build = os.path.join(root, "build")
        self.docs = os.path.join(root, "docs")
        self.tests = os.path.join(root, "tests")
        self.work = work or os.path.join(root, "work")
        self.kernel = kernel or os.path.dirname(root)
        sys.path.insert(0, self.tools)

    def package_dirs(self) -> list[str]:
        if not os.path.isdir(self.packages):
            return []
        return sorted(d for d in os.listdir(self.packages)
                      if os.path.isfile(os.path.join(self.packages, d, "hedronpkg.toml")))

    def pkg_manifest(self, name: str) -> dict[str, Any]:
        return load_toml(os.path.join(self.packages, name, "hedronpkg.toml"))

    def rel(self, path: str) -> str:
        return os.path.relpath(path, self.root)

    def all_files(self, subdirs: Iterable[str], *, exts: tuple[str, ...] | None = None,
                  skip: tuple[str, ...] = ("__pycache__", ".git", "work", "work-test")) -> list[str]:
        out = []
        for sub in subdirs:
            base = os.path.join(self.root, sub) if not os.path.isabs(sub) else sub
            if not os.path.isdir(base):
                continue
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = [d for d in dirnames if d not in skip]
                for name in filenames:
                    path = os.path.join(dirpath, name)
                    if exts is None or name.endswith(exts):
                        out.append(path)
        return sorted(out)


# --------------------------------------------------------------------- helpers
def _sh_lines(path: str) -> list[str]:
    try:
        with open(path, encoding="utf-8", errors="strict") as fh:
            return fh.read().splitlines()
    except (OSError, UnicodeDecodeError):
        return []


def _is_script(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            return fh.read(2) == b"#!"
    except OSError:
        return False


def _shebang(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            first = fh.readline().strip()
    except OSError:
        return ""
    return first if first.startswith("#!") else ""


def _iter_manifests(ctx: Context) -> Iterable[tuple[str, dict[str, Any], str]]:
    for name in ctx.package_dirs():
        path = os.path.join(ctx.packages, name, "hedronpkg.toml")
        yield name, load_toml(path), path


# ----------------------------------------------------------------------- checks
def check_manifests(ctx: Context) -> list[Check]:
    """Every package manifest parses and declares what we require of it."""
    out: list[Check] = []
    problems: list[str] = []
    seen_names: dict[str, str] = {}
    rows: list[str] = []
    for name, manifest, path in _iter_manifests_safe(ctx):
        pkg = manifest.get("package", {})
        for key in ("name", "version", "summary", "license"):
            if key not in pkg:
                problems.append("%s: [package] missing %r" % (name, key))
        declared = pkg.get("name", name)
        if declared != name:
            problems.append("%s: [package].name is %r but the directory is %r" % (name, declared, name))
        if declared in seen_names:
            problems.append("%s: duplicate package name (also in %s)" % (name, seen_names[declared]))
        seen_names[declared] = name
        version = str(pkg.get("version", ""))
        if not re.fullmatch(r"\d+(\.\d+)*([a-z]\d*)?(\.\d+)?", version):
            problems.append("%s: version %r is not a valid pkgver (no letters at the start, no dashes)"
                            % (name, version))
        summary = str(pkg.get("summary", ""))
        if not 10 <= len(summary) <= 110:
            problems.append("%s: summary is %d chars (keep it between 10 and 110 — pacman truncates the desc)"
                            % (name, len(summary)))
        if not manifest.get("install"):
            problems.append("%s: no [install] section — the package would be empty" % name)
        rows.append("%s %s-%s" % (name, version, pkg.get("release", "1")))
    out.append(Check("packages/manifests", not problems,
                     "%d package manifest(s) parsed; %d issue(s)" % (len(rows), len(problems)),
                     "\n".join(problems[:20]) if problems else "", "error"))
    # Each manifest's [install] targets must exist in the tree, and each file must not be
    # claimed by two packages (that breaks pacman with an "already exists on filesystem" error
    # during install, which is the worst possible time to discover it).
    missing: list[str] = []
    owners: dict[str, str] = {}
    for name, manifest, _path in _iter_manifests_safe(ctx):
        install = manifest.get("install", {}) or {}
        srcdir = os.path.join(ctx.packages, name)
        tree = install.get("tree")
        if tree and not os.path.isdir(os.path.join(srcdir, tree)):
            missing.append("%s: install.tree = %r does not exist" % (name, tree))
        for entry in (install.get("files") or []) + (install.get("copies") or []):
            for side in ("src", "dst"):
                if side == "dst":
                    continue
                if not os.path.exists(os.path.join(srcdir, entry[side])):
                    missing.append("%s: install file src %r missing" % (name, entry[side]))
        for entry in (install.get("vendored") or []):
            source = entry["from"]
            cands = [os.path.join(srcdir, source), os.path.join(ctx.tools, source),
                     os.path.join(ctx.root, source)]
            if not any(os.path.isdir(c) for c in cands):
                missing.append("%s: vendored source %r not found in %s" % (name, source, srcdir))
        root = os.path.join(srcdir, tree) if tree else None
        if root and os.path.isdir(root):
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if d != "__pycache__"]
                for fname in filenames:
                    rel = "/" + os.path.relpath(os.path.join(dirpath, fname), root).replace(os.sep, "/")
                    if rel.startswith("/hedronlib/"):
                        continue
                    owners.setdefault(rel, name)
    out.append(Check("packages/install-sources", not missing,
                     "all trees, files and vendored sources resolve",
                     "\n".join(missing[:15]), "error"))
    return out


def _iter_manifests_safe(ctx: Context):
    for name in ctx.package_dirs():
        path = os.path.join(ctx.packages, name, "hedronpkg.toml")
        try:
            yield name, load_toml(path), path
        except Exception as exc:  # noqa: BLE001 - reported below
            yield name, {"__parse_error__": str(exc)}, path


def check_file_collisions(ctx: Context) -> list[Check]:
    """Two packages must not install the same path (except deliberately shared data)."""
    owners: dict[str, str] = {}
    problems: list[str] = []
    for name, manifest, _path in _iter_manifests_safe(ctx):
        if "__parse_error__" in manifest:
            continue
        install = manifest.get("install", {}) or {}
        tree = install.get("tree")
        srcdir = os.path.join(ctx.packages, name)
        root = os.path.join(srcdir, tree) if tree else None
        if root and os.path.isdir(root):
            for dirpath, _dirnames, filenames in os.walk(root):
                for fname in filenames:
                    full = os.path.join(dirpath, fname)
                    rel = "/" + os.path.relpath(full, root).replace(os.sep, "/")
                    if rel.endswith(("/README.md", "/LICENSE")) or "/hedronlib/" in rel:
                        continue  # per-package docs and vendored library copies are expected
                    if rel in owners and owners[rel] != name:
                        problems.append("%s claimed by %s and %s" % (rel, owners[rel], name))
                    owners[rel] = name
    return [Check("packages/no-path-collisions", not problems,
                  "%d distinct paths across %d packages" % (len(owners), len(ctx.package_dirs())),
                  "\n".join(problems[:20]), "error")]


def check_python(ctx: Context) -> list[Check]:
    """Everything we ship must at least compile, and no shipped file may be a stub."""
    bad: list[str] = []
    stubs: list[str] = []
    files = ctx.all_files(("tools", "packages", "tests", "build"), exts=(".py",))
    files += [f for f in ctx.all_files(("packages",)) if _is_script(f)]
    n = 0
    for path in files:
        if not path.endswith(".py") and "python" not in _shebang(path):
            continue
        try:
            with _TMP_CT:
                py_compile.compile(path, doraise=True, cfile=os.path.join(_TMP_CT.name, "x.pyc"))
            n += 1
        except py_compile.PyCompileError as exc:
            bad.append("%s: %s" % (ctx.rel(path), str(exc).splitlines()[-1][:160]))
        text = "\n".join(_sh_lines(path))
        markers = ("NotImplemented" + "Error", "raise NotImplemented", "TODO: implement", "XXX: stub")
        for marker in markers:
            if marker in text and "/tests/" not in path and ctx.rel(path) != "tools/hedronlib/verify.py":
                stubs.append("%s: contains %r" % (ctx.rel(path), marker))
    checks = [Check("python/compiles", not bad, "%d module(s) compile-checked" % n,
                     "\n".join(bad[:12]), "error")]
    if stubs:
        checks.append(Check("python/no-stubs", False, "%d suspected stub(s)" % len(stubs),
                            "\n".join(stubs[:12])))
    return checks


def check_shell(ctx: Context) -> list[Check]:
    """bash -n on every shell script, and no inline python heredocs in build libs."""
    bad: list[str] = []
    files = [f for f in ctx.all_files(("build", "tools", "packages")) if f.endswith(".sh")]
    files += [f for f in ctx.all_files(("packages", "tools", "build"))
              if not f.endswith(".sh") and _shebang(f).endswith(("sh", "bash")) and os.path.isfile(f)]
    for path in files:
        proc = run(["bash", "-n", path])
        if proc.returncode != 0:
            bad.append("%s: %s" % (ctx.rel(path), (proc.stderr or "").strip().splitlines()[-1:] or [""])[0][:180])
    inline = []
    for path in glob.glob(os.path.join(ctx.build, "lib", "*.sh")):
        text = "\n".join(_sh_lines(path))
        for pat in (r"python3?\s+-\s*<<", r"python3?\s+<<", r"\bawk\s+-f\s*-\s*<<"):
            if re.search(pat, text):
                inline.append("%s: inline interpreter heredoc (matches %s) — move it into tools/"
                              % (ctx.rel(path), pat))
    crlf = [ctx.rel(p) for p in files if b"\r\n" in open(p, "rb").read(8192)]
    out = [Check("shell/syntax", not bad, "%d shell script(s) pass bash -n" % len(files),
                 "\n".join(bad[:12]), "error")]
    if inline or crlf:
        out.append(Check("shell/style", False, "%d structural issue(s)" % (len(inline) + len(crlf)),
                         "\n".join(inline[:8] + ["%s: CRLF line endings" % c for c in crlf[:4]]), "error"))
    return out


def check_catalog(ctx: Context) -> list[Check]:
    from .catalog import Catalog  # local import: only needed for this check
    path = os.path.join(ctx.data, "apps.toml")
    if not os.path.isfile(path):
        return [Check("apps/catalog", False, "data/apps.toml missing", "", "error")]
    problems: list[str] = []
    try:
        cat = Catalog.load(path)
        problems = list(cat.validate())
    except Exception as exc:  # noqa: BLE001
        return [Check("apps/catalog", False, "catalog failed to load", str(exc)[:400], "error")]
    # The gaming promise: Steam must exist, be locked, and be the default selection.
    steam = cat.apps.get("steam")
    if steam is None:
        problems.append("no app with id 'steam' in the catalogue — the installer contract requires it")
    else:
        if not getattr(steam, "locked", False):
            problems.append("steam must be locked (it cannot be unticked in the chooser)")
    for profile in ("gamerte", "desktop", "handheld", "studio", "core"):
        try:
            sel = cat.default_selection(profile)
        except Exception as exc:  # noqa: BLE001
            problems.append("default_selection(%s) raised %s" % (profile, exc))
            continue
        if profile in ("gamerte", "handheld") and "steam" not in set(sel):
            problems.append("profile %s must pre-select steam (got %d ids)" % (profile, len(sel)))
    counts = {"apps": len(cat.visible()), "categories": len(cat.categories), "bundles": len(cat.bundles),
              "problems": len(problems)}
    if counts["apps"] < 40:
        problems.append("only %d apps in the catalogue (the promise was 'lots of cool apps')" % counts["apps"])
    return [Check("apps/catalog", not problems,
                  "%(apps)d apps, %(categories)d categories, %(bundles)d bundles, "
                  "%(locked)s locked; %(problems)d problem(s)" % dict(counts, locked=len(cat.locked_ids())),
                  "\n".join(problems[:20]), "error")]


def check_package_sets(ctx: Context) -> list[Check]:
    from .pkgsets import load_sets  # local import
    problems: list[str] = []
    resolved: list[str] = []
    from .pkgsets import Resolver  # local import
    try:
        raw, hedron = load_sets(ctx.data, ctx.packages)
        sets = Resolver(raw, hedron)
    except Exception as exc:  # noqa: BLE001
        return [Check("packages/sets", False, "package-sets.toml failed to load", str(exc)[:400], "error")]
    for profile in sets.profile_names():
        for variant in ("dvd", "net", "uc"):
            try:
                res = sets.resolve(profile, variant)
            except Exception as exc:  # noqa: BLE001
                problems.append("%s/%s raised %s: %s" % (profile, variant, type(exc).__name__, str(exc)[:160]))
                continue
            if res.errors:
                problems.extend("%s/%s: %s" % (profile, variant, e) for e in res.errors)
            if not res.packages:
                problems.append("%s/%s resolved to zero packages" % (profile, variant))
            if "steam" not in " ".join(res.names()):
                if profile in ("gamerte", "handheld") and variant != "uc":
                    problems.append("%s/%s does not include steam" % (profile, variant))
            resolved.append("%s/%s=%d" % (profile, variant, len(res.packages)))
    return [Check("packages/sets", not problems,
                  "5 profiles × 3 variants resolved: " + ", ".join(resolved[:15]),
                  "\n".join(problems[:20]), "error")]


def check_gpu_db(ctx: Context) -> list[Check]:
    from .gpus import GpuDatabase  # local import
    db_path = os.path.join(ctx.data, "gpu-db", "gpu-db.json")
    checks: list[Check] = []
    if not os.path.isfile(db_path):
        return [Check("gpu/db", False, "data/gpu-db/gpu-db.json missing (run make gpu-db)", "", "error")]
    try:
        db = GpuDatabase(load_json(db_path))
    except Exception as exc:  # noqa: BLE001
        return [Check("gpu/db", False, "gpu-db.json failed to load", str(exc)[:400], "error")]
    problems: list[str] = []
    if db.schema != 3:
        problems.append("schema is %r, expected 3 (bump the loaders together)" % db.schema)
    counts = {vendor: len(db.raw.get(vendor, []) or []) for vendor in ("amd", "intel", "nvidia")}
    if counts.get("amd", 0) < 300 or counts.get("intel", 0) < 80 or counts.get("nvidia", 0) < 150:
        problems.append("suspiciously small table for a whole-distro GPU database: %s" % counts)
    sidecar = os.path.join(os.path.dirname(db_path), "gpu-db.sha256")
    if os.path.isfile(sidecar):
        want = open(sidecar, encoding="utf-8").read().split()[0].strip()
        got = sha256_file(db_path)
        if want != got:
            problems.append("gpu-db.sha256 mismatch: the JSON was hand-edited instead of regenerated "
                            "(recorded %s, actual %s)" % (want[:12], got[:12]))
    else:
        problems.append("no gpu-db.sha256 sidecar; regenerate")
    # A lookup that cannot find anything is the real failure mode, so probe known IDs.
    probes = {"0x1002:0x744C": "amd", "0x8086:0x56A0": "intel", "0x10DE:0x2684": "nvidia",
              "0x10DE:0x0FF9": "nvidia"}
    from .gpus import PciDevice  # local import: the probe below speaks the loader's language
    for key, vendor in probes.items():
        vid, did = (int(x, 16) for x in key.split(":"))
        try:
            ident = db.lookup(PciDevice(vendor=vid, device=did))
        except Exception as exc:  # noqa: BLE001
            problems.append("lookup(%s) raised %s" % (key, exc))
            continue
        if ident is None:
            problems.append("lookup(%s) returned nothing (expected %s)" % (key, vendor))
        elif ident.vendor != vendor:
            problems.append("lookup(%s) → %s, expected %s" % (key, ident.vendor, vendor))
    checks.append(Check("gpu/db", not problems, "gpu-db loaded: %s" % (counts or "n/a"),
                        "\n".join(problems[:15]), "error"))
    return checks


def check_driver_rules(ctx: Context) -> list[Check]:
    from .drivers import plan_all_families  # local import
    from .gpus import GpuDatabase
    rules_path = os.path.join(ctx.data, "driver-rules.toml")
    db_path = os.path.join(ctx.data, "gpu-db", "gpu-db.json")
    if not os.path.isfile(rules_path):
        return [Check("drivers/rules", False, "data/driver-rules.toml missing", "", "error")]
    problems: list[str] = []
    try:
        rules = load_toml(rules_path)
    except Exception as exc:  # noqa: BLE001
        return [Check("drivers/rules", False, "driver-rules.toml does not parse", str(exc)[:300], "error")]
    fmt = str(rules.get("format", ""))
    if not fmt.startswith(("hedronos-driver-rules", "hedron-driver-rules")):
        problems.append("format = %r — the engine refuses a rules file it cannot identify" % fmt)
    if not rules.get("version"):
        problems.append("[driver-rules] needs a version so a new engine can refuse an old rules file")
    vendors = rules.get("vendors", {}) or {}
    for vendor in ("nvidia", "amd", "intel", "qemu"):
        if vendor not in vendors:
            problems.append("no [vendors.%s] branch — GPUs of that vendor would get no plan" % vendor)
            continue
    # The strongest check here: every family in the generated DB must select a branch, and
    # every branch must name at least one package that exists somewhere we can install from.
    known = set(ctx.package_dirs())
    try:
        db = GpuDatabase(load_json(db_path))
        plans = plan_all_families(db, rules)
    except Exception as exc:  # noqa: BLE001
        return [Check("drivers/rules", False, "plan_all_families failed", str(exc)[:300], "error")]
    if not plans:
        problems.append("plan_all_families produced nothing")
    by_vendor: dict[str, int] = {}
    kmods: set[str] = set()
    for plan in plans:
        by_vendor[plan["vendor"]] = by_vendor.get(plan["vendor"], 0) + 1
        kmods.add(plan["kmod"])
        if not plan["packages"] and not plan["fallback"]:
            problems.append("%s/%s (%s): a branch with neither packages nor fallback" %
                            (plan["vendor"], plan["family"], plan["branch"]))
    ctx._plans = plans  # type: ignore[attr-defined]
    checks = [Check("drivers/rules", not problems,
                    "%d family plans across %s; kmods: %s" % (len(plans), by_vendor, ", ".join(sorted(kmods))),
                    "\n".join(problems[:20]), "error")]
    return checks


def check_units(ctx: Context) -> list[Check]:
    from . import units  # local import
    all_units = units.load_tree(ctx.packages)
    problems: list[str] = []
    for unit in all_units:
        for err in unit.errors:
            problems.append(err)
    problems.extend(units.check_all(all_units))
    # Every unit a package claims to enable must exist in that package's own tree.
    for name, manifest, _path in _iter_manifests_safe(ctx):
        services = manifest.get("services", {}) or {}
        tree = os.path.join(ctx.packages, name, (manifest.get("install", {}) or {}).get("tree", "src"))
        for unit in services.get("enable", []) or []:
            if os.path.isdir(tree) and not glob.glob(os.path.join(tree, "**", unit), recursive=True):
                problems.append("%s: [services].enable lists %s but the package installs no such unit file"
                                % (name, unit))
        for rel in services.get("units", []) or []:
            if os.path.isdir(tree) and not os.path.isfile(os.path.join(tree, rel.lstrip("/"))):
                problems.append("%s: [services].units lists %s which is not in the tree" % (name, rel))
    return [Check("systemd/units", not problems,
                  "%d unit file(s) parsed (%s)" % (len(all_units), units.summary(all_units)["kinds"]),
                  "\n".join(problems[:20]), "error")]


def check_executables(ctx: Context) -> list[Check]:
    """Anything in usr/bin must be executable, have a shebang or be a real binary, and run."""
    problems: list[str] = []
    ran = 0
    for name in ctx.package_dirs():
        srcdir = os.path.join(ctx.packages, name)
        tree = os.path.join(srcdir, (load_toml(os.path.join(srcdir, "hedronpkg.toml")).get("install", {}) or {})
                            .get("tree", "src"))
        if not os.path.isdir(tree):
            continue
        for path in sorted(glob.glob(os.path.join(tree, "**", "usr", "bin", "*"), recursive=True)):
            if not os.path.isfile(path):
                continue
            rel = os.path.relpath(path, tree)
            st = os.stat(path)
            if not st.st_mode & 0o111:
                problems.append("%s: %s is not executable (mode %o)" % (name, rel, st.st_mode & 0o777))
            shebang = _shebang(path)
            if not shebang and open(path, "rb").read(4) not in (b"\x7fELF",):
                problems.append("%s: %s has neither a shebang nor an ELF header" % (name, rel))
            if shebang and "python" in shebang:
                proc = run([sys.executable, path, "--help"], cwd=ctx.root, timeout=60)
                ran += 1
                # --help on a CLI is a contract: exit 0. Anything else is an import error.
                if proc.returncode != 0:
                    tail = (proc.stderr or proc.stdout or "").strip().splitlines()
                    problems.append("%s: %s --help exited %d: %s"
                                    % (name, rel, proc.returncode, (tail[-1] if tail else "")[:180]))
            elif shebang and re.search(r"(^|/)(ba|z|da)?sh$", shebang):
                proc = run(["bash", path, "--help"], cwd=ctx.root, timeout=60)
                ran += 1
                if proc.returncode not in (0, 1, 100, 2):
                    problems.append("%s: %s --help exited %d" % (name, rel, proc.returncode))
    return [Check("packages/executables", not problems,
                  "%d user-facing CLI(s) executed with --help" % ran, "\n".join(problems[:20]), "error")]


def check_features(ctx: Context) -> list[Check]:
    path = os.path.join(ctx.packages, "hedronos-release", "src", "etc", "hedron", "features.json")
    if not os.path.isfile(path):
        path = os.path.join(ctx.data, "features.json")
    if not os.path.isfile(path):
        return [Check("features/registry", False, "no features.json found", "", "error")]
    try:
        doc = load_json(path)
    except Exception as exc:  # noqa: BLE001
        return [Check("features/registry", False, "features.json does not parse", str(exc)[:300], "error")]
    problems: list[str] = []
    ids: set[str] = set()
    packages = set(ctx.package_dirs())
    for feat in doc.get("features", []):
        fid = feat.get("id", "")
        if not fid or fid in ids:
            problems.append("feature with missing/duplicate id %r" % fid)
        ids.add(fid)
        for key in ("name", "package", "summary", "probe"):
            if not feat.get(key):
                problems.append("%s: missing %r" % (fid, key))
        pkg = feat.get("package", "")
        if pkg and pkg not in packages:
            problems.append("%s: package %r is not in packages/ (feature would be unshippable)" % (fid, pkg))
        probe = feat.get("probe", "")
        if len(probe) > 240:
            problems.append("%s: probe is %d chars (keep it a one-liner)" % (fid, len(probe)))
        if "&&" not in probe and "||" not in probe and "test" not in probe and "command -v" not in probe \
                and "systemctl" not in probe:
            problems.append("%s: probe %r does not look like a check" % (fid, probe[:60]))
    if len(doc.get("features", [])) < 8:
        problems.append("only %d features listed — this is supposed to be the differentiator list"
                        % len(doc.get("features", [])))
    return [Check("features/registry", not problems,
                  "%d feature(s) declared, %d package(s) referenced" % (len(doc.get("features", [])),
                                                                      len({f.get("package") for f in doc.get("features", [])})),
                  "\n".join(problems[:20]), "error")]


def check_boot_entries(ctx: Context) -> list[Check]:
    path = os.path.join(ctx.data, "boot-entries.toml")
    if not os.path.isfile(path):
        return [Check("boot/entries", False, "data/boot-entries.toml missing", "", "error")]
    problems: list[str] = []
    try:
        spec = load_toml(path)
    except Exception as exc:  # noqa: BLE001
        return [Check("boot/entries", False, "boot-entries.toml does not parse", str(exc)[:300], "error")]
    entries = spec.get("entries", []) or []
    ids = [e.get("id", "") for e in entries]
    if len(ids) != len(set(ids)):
        problems.append("duplicate entry ids (systemd-boot names files after them)")
    if not entries:
        problems.append("no boot entries at all")
    for entry in entries:
        for key in ("id", "title"):
            if not entry.get(key):
                problems.append("entry %r missing %s" % (entry.get("id"), key))
        if entry.get("linux") and not entry["linux"].startswith(("/", "@")):
            problems.append("entry %s: linux= must be an absolute path inside the ESP" % entry.get("id"))
    timeout = spec.get("timeout")
    if timeout is None or not str(timeout).isdigit():
        problems.append("timeout must be a number of seconds — a menu you cannot reach is a "
                        "support ticket (got %r)" % (timeout,))
    elif int(str(timeout)) > 30:
        problems.append("timeout=%s s is long enough that a typo makes the ISO feel broken" % timeout)
    outdir = "/tmp/hedron-boot-check"
    proc = run([sys.executable, os.path.join(ctx.tools, "hedron-boot-entries.py"), "--spec", path,
                "--profile", "gamerte", "--variant", "dvd", "--version", "0.0-test", "--codename", "T",
                "--outdir", outdir, "--loader-conf", os.path.join(outdir, "loader.conf"),
                "--grub-out", os.path.join(outdir, "grub.cfg"), "--print"], cwd=ctx.root, timeout=90)
    if proc.returncode != 0:
        problems.append("the renderer failed: %s" % ((proc.stderr or proc.stdout).strip().splitlines() or [""])[-1][:200])
    else:
        text = proc.stdout
        if "hedronos" not in text.lower():
            problems.append("rendered entries do not mention hedronos")
    return [Check("boot/entries", not problems, "%d entry/entries rendered by the shipped renderer" % len(entries),
                  "\n".join(problems[:15]), "error")]


def check_scripts_run(ctx: Context) -> list[Check]:
    """The build entry point must work in dry-run mode right now."""
    problems: list[str] = []
    build_sh = os.path.join(ctx.root, "build.sh")
    if not os.path.isfile(build_sh):
        return [Check("build/dry-run", False, "build.sh missing from the distro root", "", "error")]
    work = os.path.join(ctx.work, "verify-dryrun")
    proc = run(["bash", build_sh, "--dry-run", "--profile", "gamerte", "--variant", "dvd",
                "--kernel", "host", "--out", os.path.join(ctx.work, "verify-out"),
                "--work", work], cwd=ctx.root, timeout=900)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        problems.append("build.sh --dry-run exited %d: %s" % (proc.returncode, (tail[-3:] or [""])[0][:200]))
    log = os.path.join(work, "dryrun", "dry-run.log")
    if not os.path.isfile(log):
        problems.append("no dry-run log written to %s" % log)
    else:
        lines = _sh_lines(log)
        fails = [l for l in lines if re.search(r"\bFAIL\b|\bERROR\b|unbound variable", l)]
        if fails:
            problems.append("%d failing line(s) in the dry-run log: %s" % (len(fails), fails[0][:180]))
        for stage in ("preflight", "gpu-db", "packages", "rootfs", "drivers-preseed", "bootloader", "iso"):
            if not any(stage in l for l in lines):
                problems.append("dry-run log never mentions the %s stage" % stage)
    return [Check("build/dry-run", not problems, "build.sh --dry-run completed with a full stage log",
                  "\n".join(problems[:10]), "error")]


def check_version_consistency(ctx: Context) -> list[Check]:
    """VERSION at the root, every package, and the README must agree."""
    path = os.path.join(ctx.root, "VERSION")
    if not os.path.isfile(path):
        return [Check("release/version", False, "hedronos/VERSION missing", "", "error")]
    kv = {}
    for line in _sh_lines(path):
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            kv[k.strip()] = v.strip()
    lines = [l.strip() for l in _sh_lines(path) if l.strip() and not l.startswith("#")]
    # The file is `1.0.0` on line 1 then key=value pairs: the bare first line is the version
    # (that is what build/lib/preflight.sh reads), so accept both layouts here.
    version = kv.get("VERSION") or (lines[0].split()[0] if lines and "=" not in lines[0] else "")
    problems: list[str] = []
    if not version:
        problems.append("VERSION file has neither a bare version line nor a VERSION= key")
    for name, manifest, _path in _iter_manifests_safe(ctx):
        if "__parse_error__" in manifest:
            problems.append("%s: manifest does not parse (%s)" % (name, manifest["__parse_error__"]))
            continue
        got = str(manifest.get("package", {}).get("version", ""))
        if got and got != version:
            problems.append("%s: package version %r != VERSION %r" % (name, got, version))
    readme = os.path.join(ctx.root, "README.md")
    if os.path.isfile(readme) and version and version not in open(readme, encoding="utf-8").read():
        problems.append("README.md never mentions version %s (update the banner)" % version)
    codename = kv.get("codename") or kv.get("CODENAME", "")
    osrel = os.path.join(ctx.packages, "hedronos-release", "src", "etc", "os-release")
    if os.path.isfile(osrel) and codename:
        text = open(osrel, encoding="utf-8").read()
        if codename not in text:
            problems.append("etc/os-release does not carry the codename %r" % codename)
        if 'ID=hedronos' not in text:
            problems.append("etc/os-release must set ID=hedronos (installers and probes key off it)")
    return [Check("release/version", not problems,
                  "VERSION=%s codename=%s across %d package manifest(s)" % (version, codename, len(ctx.package_dirs())),
                  "\n".join(problems[:15]), "error")]


def check_sched_ext(ctx: Context) -> list[Check]:
    """The BPF scheduler must use only API that exists in *this* kernel tree."""
    sched = os.path.join(os.path.dirname(ctx.root), "tools", "sched_ext")
    bpf = os.path.join(sched, "scx_hedron.bpf.c")
    user = os.path.join(sched, "scx_hedron.c")
    if not os.path.isfile(bpf):
        return [Check("kernel/sched_ext", False, "tools/sched_ext/scx_hedron.bpf.c is missing", "", "error")]
    problems: list[str] = []
    headers = "\n".join(_sh_lines(p) for p in glob.glob(os.path.join(sched, "include", "scx", "*.h")))
    headers += "\n".join(_sh_lines(p) for p in glob.glob(os.path.join(os.path.dirname(sched), "bpf", "include", "uapi", "linux", "bpf_helpers.h")))
    text = "\n".join(_sh_lines(bpf))
    used = sorted(set(re.findall(r"\bscx_bpf_[a-z0-9_]+", text)))
    missing = [sym for sym in used if sym not in headers]
    if missing:
        problems.append("scx_bpf_* symbols not declared in this tree's headers: %s" % ", ".join(missing))
    for required in ('SEC("license")', "UEI_DEFINE(", "BPF_STRUCT_OPS(", "SCX_OPS_DEFINE("):
        if required not in text:
            problems.append("scx_hedron.bpf.c is missing %s (the loader/kernel expects it)" % required)
    if 'char _license[] SEC("license") = "GPL"' not in text:
        problems.append("licence must be exactly GPL for a BPF program touching kernel internals")
    makefile = "\n".join(_sh_lines(os.path.join(sched, "Makefile")))
    if makefile and "scx_hedron" not in makefile:
        problems.append("tools/sched_ext/Makefile does not reference scx_hedron — `make` would not build it")
    for var in ("BPF_OBJS", "ALL_TARGETS"):
        if var in makefile:
            block = makefile[makefile.index(var):makefile.index(var) + 600]
            if "scx_hedron" not in block and "hedron" not in block:
                problems.append("Makefile's %s list does not include scx_hedron" % var)
    if os.path.isfile(user):
        utext = "\n".join(_sh_lines(user))
        for required in ("sched_ext", "scx_hedron_ops"):
            if required not in utext and "hedron_ops" not in utext:
                problems.append("userspace loader never mentions %s" % required)
    else:
        problems.append("no userspace loader (scx_hedron.c) — the scheduler would be unloadable")
    return [Check("kernel/sched_ext", not problems,
                  "%d scx_bpf_* symbol(s) checked against this tree's headers" % len(used),
                  "\n".join(problems[:12]), "error")]


def check_kernel_config(ctx: Context) -> list[Check]:
    """Every symbol in the HedronOS defconfig must be a real Kconfig symbol."""
    defconfig = os.path.join(ctx.root, "kernel", "hedronos_defconfig")
    fragment = os.path.join(ctx.root, "kernel", "hedronos.fragment")
    target = defconfig if os.path.isfile(defconfig) else (fragment if os.path.isfile(fragment) else None)
    if target is None:
        return [Check("kernel/config", False, "no kernel/hedronos_defconfig or hedronos.fragment", "", "error")]
    tool = os.path.join(ctx.tools, "kconf-check.py")
    if not os.path.isfile(tool):
        return [Check("kernel/config", False, "tools/kconf-check.py missing", "", "error")]
    proc = run([sys.executable, tool, "--kernel", ctx.kernel, "--config", target, "--json"],
               cwd=ctx.root, timeout=600)
    if proc.returncode not in (0, 1):
        return [Check("kernel/config", False, "kconf-check exited %d" % proc.returncode,
                      (proc.stderr or "")[-400:], "error")]
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return [Check("kernel/config", False, "kconf-check produced no JSON", proc.stdout[-300:], "error")]
    unknown = data.get("unknown", [])
    return [Check("kernel/config", not unknown,
                  "%d symbol(s) checked, %d unknown, %d already set" % (data.get("checked", 0), len(unknown),
                                                                       data.get("already_enabled", 0)),
                  "unknown symbols: " + ", ".join(unknown[:12]), "error")]


def check_docs(ctx: Context) -> list[Check]:
    """Docs must not lie: the commands they show must exist in the tree."""
    problems: list[str] = []
    docs = sorted(glob.glob(os.path.join(ctx.docs, "*.md"))) + [os.path.join(ctx.root, "README.md")]
    docs = [d for d in docs if os.path.isfile(d)]
    if len(docs) < 3:
        problems.append("only %d doc file(s) — a gaming distro nobody can configure is a toy" % len(docs))
    bins = set()
    for name in ctx.package_dirs():
        tree = os.path.join(ctx.packages, name, "src")
        for path in glob.glob(os.path.join(tree, "**", "usr", "bin", "*"), recursive=True):
            bins.add(os.path.basename(path))
        for path in glob.glob(os.path.join(tree, "**", "usr", "lib", "hedron", "*"), recursive=True):
            bins.add(os.path.basename(path))
    cli_names = bins | {os.path.basename(p) for p in glob.glob(os.path.join(ctx.tools, "*"))}
    cli_names = {n for n in cli_names if n.startswith(("hedron", "hgm", "hstore", "mkhedron"))}
    mentions: dict[str, set[str]] = {}
    for doc in docs:
        text = open(doc, encoding="utf-8").read()
        for name in cli_names:
            if re.search(r"\b%s\b" % re.escape(name), text):
                mentions.setdefault(name, set()).add(ctx.rel(doc))
    missing_bin = sorted(n for n in ("hedron-tune", "hedron-drivers", "hstore", "hedron-doctor", "hedronctl")
                         if n in cli_names and n not in mentions)
    for name in missing_bin:
        problems.append("%s is never mentioned in any doc" % name)
    # any `command` in a fenced block that looks like one of our tools must exist
    for doc in docs:
        text = open(doc, encoding="utf-8").read()
        for cmd in re.findall(r"^\s*\$ (hedron[a-z-]*|hgm| hstore)\b", text, re.M):
            if cmd not in cli_names:
                problems.append("%s: documents %r which no package provides" % (ctx.rel(doc), cmd))
    return [Check("docs/commands", not problems,
                  "%d doc(s) covering %d CLI(s)" % (len(docs), len(mentions)), "\n".join(problems[:15]),
                  "warn")]


def check_tests(ctx: Context) -> list[Check]:
    """Run the unit-test suite with the stdlib runner (no pytest available in build roots)."""
    if not os.path.isdir(ctx.tests):
        return [Check("tests/unit", False, "tests/ directory missing", "", "error")]
    proc = run([sys.executable, "-m", "unittest", "discover", "-s", ctx.tests, "-p", "test_*.py", "-v"],
               cwd=ctx.root, timeout=900)
    tail = (proc.stderr or proc.stdout or "").strip().splitlines()
    match = re.search(r"Ran (\d+) tests in [\d.]+s", "\n".join(tail))
    count = int(match.group(1)) if match else 0
    problems = [] if proc.returncode == 0 else ["`python3 -m unittest discover` exited %d" % proc.returncode]
    if count < 40:
        problems.append("only %d test(s) collected — that is not enough for a distro" % count)
    return [Check("tests/unit", not problems, "%d test(s) passed" % count,
                  "\n".join(problems + tail[-4:]), "error")]


def check_iso_rehearsal(ctx: Context) -> list[Check]:
    """If a rehearsal ran, its manifests must be self-consistent."""
    manifest = os.path.join(ctx.work, "manifest")
    if not os.path.isdir(manifest):
        return [Check("iso/rehearsal", True, "no work/manifest yet (skipped: run `./build.sh --dry-run`)",
                      "", "info")]
    problems: list[str] = []
    pkg_list = os.path.join(manifest, "packages.txt")
    names = set()
    if os.path.isfile(pkg_list):
        names = {l.strip() for l in _sh_lines(pkg_list) if l.strip() and not l.startswith("#")}
    else:
        problems.append("manifest/packages.txt missing")
    prefetch = os.path.join(manifest, "driver-prefetch.txt")
    if os.path.isfile(prefetch):
        lines = [l for l in _sh_lines(prefetch) if l.strip()]
        if not lines:
            problems.append("driver-prefetch.txt is empty — the offline GPU promise needs packages")
    else:
        problems.append("manifest/driver-prefetch.txt missing (the drivers-preseed stage did not run)")
    plans = os.path.join(manifest, "gpu-plans.json")
    if os.path.isfile(plans):
        try:
            doc = load_json(plans)
        except Exception as exc:  # noqa: BLE001
            problems.append("gpu-plans.json unreadable: %s" % exc)
        else:
            if doc.get("format") != "hedron-gpu-plans":
                problems.append("gpu-plans.json format is %r" % doc.get("format"))
            if not doc.get("plans"):
                problems.append("gpu-plans.json has zero plans")
    else:
        problems.append("manifest/gpu-plans.json missing")
    # The rehearsal must not have silently dropped a native package.
    for name in ctx.package_dirs():
        if name.startswith("linux-"):
            continue
        if names and name not in names and "hedronos" in name:
            problems.append("%s is in packages/ but not in the resolved package list" % name)
    return [Check("iso/rehearsal", not problems, "%d package(s) in the manifest" % len(names),
                  "\n".join(problems[:15]), "error")]


def check_regeneration(ctx: Context) -> list[Check]:
    """Generated data must be reproducible from the kernel tree (no hand-edited JSON)."""
    gen = os.path.join(ctx.tools, "gen-gpu-db.py")
    db = os.path.join(ctx.data, "gpu-db", "gpu-db.json")
    if not (os.path.isfile(gen) and os.path.isfile(db)):
        return [Check("gpu/db-reproducible", False, "generator or db missing", "", "error")]
    tmp = os.path.join(ctx.work, "verify-out", "gpu-db-regen.json")
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    proc = run([sys.executable, gen, "--kernel", ctx.kernel, "--out", tmp], cwd=ctx.root, timeout=1800)
    if proc.returncode != 0:
        return [Check("gpu/db-reproducible", False, "regeneration failed", (proc.stderr or "")[-400:], "error")]
    a = sha256_text(re.sub(r'"generated[^"]*":\s*"[^"]*"', "", open(db, encoding="utf-8").read()))
    b = sha256_text(re.sub(r'"generated[^"]*":\s*"[^"]*"', "", open(tmp, encoding="utf-8").read()))
    if a != b:
        return [Check("gpu/db-reproducible", False,
                      "regenerated db differs from the committed one (%s vs %s)" % (a[:12], b[:12]),
                      "run `make gpu-db` and commit the result, or fix the generator", "error")]
    return [Check("gpu/db-reproducible", True, "regeneration is byte-identical (modulo timestamps)", "", "info")]


def check_license_headers(ctx: Context) -> list[Check]:
    """Every shipped source file carries an SPDX identifier (kernel convention)."""
    bad: list[str] = []
    n = 0
    generated = (os.path.join("data", "gpu-db", "gpu-db.json"),)
    for path in ctx.all_files(("packages", "tools", "build", "kernel", "tests", "docs"),
                              exts=(".py", ".sh", ".c", ".h", ".toml", ".json", ".qml", ".js",
                                    ".service", ".timer", ".socket", ".hook", ".rules", ".conf")):
        if any(skip in path for skip in ("__pycache__", "/work", "node_modules", "fixtures")):
            continue
        if ctx.rel(path) in generated:
            continue  # regenerated by gen-gpu-db.py; a hand-edit would break reproducibility
        text = "\n".join(_sh_lines(path)[:14])
        n += 1
        if "SPDX-License-Identifier" not in text and "SPDX" not in text:
            bad.append(ctx.rel(path))
    return [Check("meta/spdx-headers", not bad,
                  "%d source file(s) checked, %d missing a licence tag" % (n, len(bad)),
                  "\n".join(bad[:20]), "warn")]


CHECKS: list[tuple[str, Callable[[Context], list[Check]]]] = [
    ("manifests", check_manifests),
    ("collisions", check_file_collisions),
    ("python", check_python),
    ("shell", check_shell),
    ("catalog", check_catalog),
    ("package-sets", check_package_sets),
    ("gpu-db", check_gpu_db),
    ("driver-rules", check_driver_rules),
    ("units", check_units),
    ("executables", check_executables),
    ("features", check_features),
    ("boot", check_boot_entries),
    ("kernel-config", check_kernel_config),
    ("sched-ext", check_sched_ext),
    ("docs", check_docs),
    ("tests", check_tests),
    ("iso", check_iso_rehearsal),
    ("version", check_version_consistency),
    ("licence", check_license_headers),
    ("regeneration", check_regeneration),
    ("build", check_scripts_run),
]


def run_all(ctx: Context, only: Iterable[str] | None = None, skip: Iterable[str] = ()) -> list[Check]:
    only = set(only) if only else None
    skip = set(skip)
    results: list[Check] = []
    for group, fn in CHECKS:
        if only and group not in only:
            continue
        if group in skip:
            continue
        try:
            results.extend(fn(ctx))
        except Exception as exc:  # noqa: BLE001 - a crashing check is itself a finding
            results.append(Check("%s/crashed" % group, False,
                                 "%s: %s" % (type(exc).__name__, exc),
                                 "the check itself raised; fix it, do not ignore it", "error"))
    return results


def summarise(checks: list[Check]) -> dict[str, Any]:
    return {"total": len(checks),
            "passed": sum(1 for c in checks if c.ok),
            "failed": sum(1 for c in checks if not c.ok and c.severity == "error"),
            "warnings": sum(1 for c in checks if not c.ok and c.severity == "warn"),
            "info": sum(1 for c in checks if c.ok and c.severity == "info")}
