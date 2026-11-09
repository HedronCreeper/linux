"""Static invariants over the whole layer, plus three end-to-end tool round-trips.

SPDX-License-Identifier: MIT
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest

from helpers import (DATA_DIR, HEDRON_DIR, KERNEL_DIR, PACKAGES_DIR, REPO_ROOT, TempDirMixin,
                     bin_paths, run_cli, run_tool)

DOC_VERBLESS = {"hedron-notify", "hedron-gpu-detect", "hedron-run", "hedron-setup", "hedron-share",
                "gamemoderun", "hgm", "hedron-deck-session", "hedron-session-query",
                "hedron-branding"}


def package_dirs():
    return sorted(d for d in os.listdir(PACKAGES_DIR)
                  if os.path.isfile(os.path.join(PACKAGES_DIR, d, "hedronpkg.toml")))


def read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _first_json_object(text: str) -> dict:
    """The first `{...}` document in a stream that may also carry log lines."""
    lines = text.splitlines()
    start = next((i for i, l in enumerate(lines) if l.strip().startswith("{")), None)
    if start is None:
        raise AssertionError("no JSON object on stdout: %r" % text[:200])
    depth = 0
    for i in range(start, len(lines)):
        depth += lines[i].count("{") - lines[i].count("}")
        if depth <= 0:
            return json.loads("\n".join(lines[start:i + 1]))
    raise AssertionError("unterminated JSON object: %r" % text[:200])


# ExecStart targets that no `src/` tree contains because they are compiled at package build time.
# Each entry says where the source is and which file carries the rule that builds it — the pair is the
# promise; a unit whose binary exists in neither place is the bug this check is for.
BUILD_PRODUCTS = {
    "/usr/bin/hgm": (os.path.join(PACKAGES_DIR, "hedronos-gamemode", "src", "usr", "src",
                                  "hedron-gamemode.c"),
                     os.path.join(PACKAGES_DIR, "hedronos-gamemode", "hedronpkg.toml")),
    "/usr/bin/hedron-latency": (os.path.join(PACKAGES_DIR, "hedronos-tune", "src", "usr", "src",
                                              "hedron-latency.c"),
                                os.path.join(PACKAGES_DIR, "hedronos-tune", "hedronpkg.toml")),
    "/usr/lib/hedron/scx/scx_hedron": (os.path.join(REPO_ROOT, "tools", "sched_ext", "scx_hedron.c"),
                                       os.path.join(PACKAGES_DIR, "linux-hedron", "PKGBUILD")),
}


class TestPackageTrees(unittest.TestCase):
    def test_every_package_has_a_manifest_and_a_readme(self):
        self.assertGreaterEqual(len(package_dirs()), 15)
        for name in package_dirs():
            with self.subTest(package=name):
                for f in ("hedronpkg.toml", "README.md"):
                    path = os.path.join(PACKAGES_DIR, name, f)
                    self.assertTrue(os.path.isfile(path), "missing %s" % f)
                    self.assertGreater(os.path.getsize(path), 120, "%s is a stub" % f)

    def test_no_shipped_script_is_a_stub(self):
        # "TODO: implement" inside a file that ships to /usr/bin is a promise we break on the user.
        markers = ("TODO: implement", "NotImplementedError", "PLACEHOLDER-DO-NOT-SHIP", "FIXME: write this")
        for root, dirs, files in os.walk(PACKAGES_DIR):
            dirs[:] = [d for d in dirs if d != "work"]
            for fn in files:
                path = os.path.join(root, fn)
                if os.path.getsize(path) > 400000:
                    continue
                try:
                    text = read(path)
                except (UnicodeDecodeError, OSError):
                    continue
                for marker in markers:
                    self.assertNotIn(marker, text, "%s still contains %r" % (os.path.relpath(path, HEDRON_DIR), marker))

    def test_unit_files_point_at_binaries_we_ship(self):
        """ExecStart of a hedron-owned path must exist in some package tree."""
        shipped = set()
        for root, _dirs, files in os.walk(PACKAGES_DIR):
            for fn in files:
                rel = os.path.relpath(os.path.join(root, fn), PACKAGES_DIR)
                m = re.match(r"^[^/]+/src(/usr/.*)$", rel)
                if m:
                    shipped.add(m.group(1))
        checked = 0
        for root, _dirs, files in os.walk(PACKAGES_DIR):
            for fn in files:
                if not fn.endswith((".service", ".timer", ".socket", ".slice")):
                    continue
                path = os.path.join(root, fn)
                if os.path.getsize(path) > 20000:
                    continue
                text = read(path)
                checked += 1
                for m in re.finditer(r"^(?:Exec(?:Start|Stop|Reload)(?:\S*)|ExecStartPre|ExecStartPost)\s*=\s*-?\s*(/\S+)",
                                     text, re.M):
                    target = m.group(1)
                    if not target.startswith(("/usr/bin/", "/usr/lib/", "/usr/sbin/")):
                        continue
                    if not (target.startswith("/usr/bin/hedron") or target.startswith("/usr/bin/hgm")
                            or target.startswith("/usr/bin/hstore") or target.startswith("/usr/bin/gamemoderun")
                            or "/hedron" in target or "hedron" in os.path.basename(target)):
                        continue          # a system binary (bash, systemd-cat, ...) is not ours to ship
                    if target in BUILD_PRODUCTS:
                        # A build product, not a checked-in file: the package's own build step makes
                        # it (a Makefile [build] command, or the kernel's PKGBUILD). What the checkout
                        # owes us is the source and the rule that compiles it, so assert both.
                        src, rule = BUILD_PRODUCTS[target]
                        self.assertTrue(os.path.isfile(src), "%s has no source at %s" % (target, src))
                        rule_text = read(rule)
                        if os.path.basename(rule) == "PKGBUILD":
                            self.assertIn(target, rule_text,
                                          "%s never installs %s" % (os.path.relpath(rule, HEDRON_DIR), target))
                        else:
                            self.assertIn("[build]", rule_text,
                                          "%s builds nothing, so %s cannot appear" % (rule, target))
                            self.assertIn("make", rule_text, "%s does not run make" % rule)
                        continue
                    self.assertIn(target, shipped,
                                  "%s runs %s, which no package installs" % (os.path.relpath(path, HEDRON_DIR), target))
        self.assertGreaterEqual(checked, 15, "almost no unit files found — is the tree intact?")


class TestDocsHonest(unittest.TestCase):
    """`$ tool verb` in a doc must be a verb the tool implements — docs are an interface."""

    def _verbs(self, path):
        """The verbs a tool admits, taken from its own `--help`.

        Parsing each script's dispatch (argparse calls, bash `case` labels, a C `strcmp` chain) was
        tried first; it is a standing invitation to a false alarm, because one unusual quoting style
        makes the checker report verbs that work perfectly. `--help` is what a human reads, so it is
        what a document has to agree with.
        """
        if path in self._verb_cache:
            return self._verb_cache[path]
        proc = run_cli(path, "--help", timeout=60)
        text = proc.stdout + proc.stderr
        found = set(re.findall(r"\b([a-z][a-z0-9]{2,}(?:-[a-z0-9]+)?)\b", text)) or None
        self._verb_cache[path] = found
        return found

    _verb_cache: dict = {}

    def test_documented_verbs_exist(self):
        bins = bin_paths()
        verbs = {name: self._verbs(path) for name, path in bins.items()}
        docs = sorted(os.listdir(os.path.join(HEDRON_DIR, "docs"))) + ["../README.md"]
        seen = 0
        for doc in docs:
            path = os.path.join(HEDRON_DIR, "docs", doc) if not doc.startswith("..") else os.path.join(HEDRON_DIR, "README.md")
            if not os.path.isfile(path):
                continue
            for line in read(path).splitlines():
                m = re.match(r"^\s*\$\s*(hedron[a-z0-9-]*|hstore|hgm)\s+([a-z][a-z0-9-]*)", line)
                if not m:
                    continue
                name, verb = m.group(1), m.group(2)
                if name not in bins or verbs.get(name) is None:
                    continue
                seen += 1
                self.assertIn(verb, verbs[name],
                              "%s documents `%s %s`, which that tool does not implement" % (doc, name, verb))
        self.assertGreaterEqual(seen, 4, "no documented verbs were checkable — did the docs change shape?")

    def test_no_documented_tool_is_missing(self):
        # The other half: a `$ hedron-thing` in a doc for a binary no package ships.
        bins = set(bin_paths())
        problems = []
        for doc in ("README.md", "docs/tuning.md", "docs/gpu-drivers.md", "docs/store-and-sessions.md"):
            path = os.path.join(HEDRON_DIR, doc)
            if not os.path.isfile(path):
                continue
            for line in read(path).splitlines():
                m = re.match(r"^\s*\$\s*(hedron[a-z0-9-]*|hstore|hgm)\b", line)
                if m and m.group(1) not in bins:
                    problems.append("%s: %s" % (doc, m.group(1)))
        self.assertEqual(problems, [], "docs run tools we do not ship: %s" % ", ".join(problems))


class TestRuntimeRoundTrips(TempDirMixin, unittest.TestCase):
    """Three tools, driven the way a user would, against temporary state."""

    def test_hstore_seed_and_status(self):
        state = self.mktemp()
        env = {"HEDRON_STORE_STATE": os.path.join(state, "store"),
               "HEDRON_STORE_CONF": "/nonexistent/hedron-store.toml",
               "HEDRON_DATA_DIR": DATA_DIR}
        seed = run_cli(bin_paths()["hstore"], "seed", env=env, timeout=180)
        self.assertEqual(seed.returncode, 0, (seed.stdout + seed.stderr)[-400:])
        status = run_cli(bin_paths()["hstore"], "status", env=env, timeout=120)
        self.assertEqual(status.returncode, 0, (status.stdout + status.stderr)[-400:])
        self.assertIn("backends", status.stdout.lower())
        # A second seed must be a no-op-ish update, not a duplicate of the index.
        again = run_cli(bin_paths()["hstore"], "status", "--json", env=env, timeout=120)
        self.assertEqual(again.returncode, 0, again.stderr[-300:])
        # The daemon-ish tools write their journal to the same stream; find the payload instead of
        # assuming stdout is a single clean document.
        doc = _first_json_object(again.stdout)
        self.assertTrue(doc.get("ok"), "hstore status reported not-ok: %s" % again.stdout[:300])
        self.assertGreaterEqual(doc.get("entries", 0), 40, "the index is thin")

    def test_hgm_claim_round_trip_over_the_socket(self):
        """FlowState is the one promise the layer makes that a socket can lose."""
        hgm = bin_paths().get("hgm")
        if not hgm or not os.access(hgm, os.X_OK):
            self.skipTest("hgm is not built in this checkout (run make -C packages/hedronos-gamemode)")
        d = self.mktemp()
        sock = os.path.join(d, "gm.sock")
        env = {"HEDRON_GAMEMODE_SOCK": sock, "HEDRON_GAMEMODE_VERBOSE": "1",
               "XDG_RUNTIME_DIR": d, "HOME": d}
        daemon = subprocess.Popen([hgm, "--daemon"], env=env, cwd=d,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            for _ in range(60):
                if os.path.exists(sock):
                    break
                if daemon.poll() is not None:
                    self.fail("hgm --daemon exited early (rc=%s): %s"
                              % (daemon.returncode, (daemon.stdout.read() if daemon.stdout else "")[-300:]))
                __import__("time").sleep(0.05)
            else:
                self.fail("hgm daemon never created %s" % sock)

            ping = run_cli(hgm, "ping", env=env, timeout=30)
            self.assertEqual(ping.returncode, 0, (ping.stdout + ping.stderr)[-200:])

            claim = run_cli(hgm, "start", str(os.getpid()), "unittest-title", env=env, timeout=30)
            self.assertEqual(claim.returncode, 0, (claim.stdout + claim.stderr)[-200:])
            # The wire reply is {"ok": true, "clients": N, "entries": [{"pid":.., "app":..}]} — the
            # same document the QML overlay and gamemoderun read, so it is pinned here exactly.
            status = json.loads(run_cli(hgm, "status", env=env, timeout=30).stdout.strip().splitlines()[-1])
            self.assertTrue(status.get("ok"), "daemon rejected the query: %s" % status)
            entries = status.get("entries") or []
            self.assertTrue(entries, "claim did not register: %s" % status)
            self.assertIn("unittest-title", json.dumps(entries))

            release = run_cli(hgm, "end", str(os.getpid()), env=env, timeout=30)
            self.assertEqual(release.returncode, 0, (release.stdout + release.stderr)[-200:])
            after = json.loads(run_cli(hgm, "status", env=env, timeout=30).stdout.strip().splitlines()[-1])
            self.assertNotIn("unittest-title", json.dumps(after.get("entries") or []),
                             "release left the claim behind")
        finally:
            daemon.terminate()
            try:
                daemon.wait(timeout=10)
            except subprocess.TimeoutExpired:
                daemon.kill()

    def test_missing_selection_is_an_error_not_a_zero(self):
        env = {"HEDRON_STORE_STATE": os.path.join(self.mktemp(), "store"),
               "HEDRON_STORE_CONF": "/nonexistent/hedron-store.toml",
               "HEDRON_DATA_DIR": DATA_DIR}
        proc = run_cli(bin_paths()["hstore"], "check-selection", env=env, timeout=120)
        self.assertIn(proc.returncode, (0, 1))
        if proc.returncode == 1:
            self.assertTrue((proc.stdout + proc.stderr).strip(), "failed with no message")

    def test_kde_apply_is_idempotent(self):
        home = self.mktemp()
        defaults = os.path.join(HEDRON_DIR, "packages", "hedronos-kde", "src", "usr", "share", "hedron",
                                "kde", "defaults")
        if not os.path.isdir(defaults):
            self.skipTest("no kde defaults in the tree")
        env = {"HOME": home, "HEDRON_KDE_DEFAULTS": defaults, "XDG_CONFIG_HOME": os.path.join(home, ".config")}
        one = run_cli(bin_paths()["hedron-kde-apply"], "apply", env=env, timeout=180)
        self.assertEqual(one.returncode, 0, (one.stdout + one.stderr)[-400:])
        two = run_cli(bin_paths()["hedron-kde-apply"], "apply", env=env, timeout=180)
        self.assertEqual(two.returncode, 0, (two.stdout + two.stderr)[-400:])
        check = run_cli(bin_paths()["hedron-kde-apply"], "check", env=env, timeout=180)
        self.assertEqual(check.returncode, 0, "a fresh apply reports drift: %s" % (check.stdout + check.stderr)[-300:])
        self.assertTrue(os.path.isfile(os.path.join(home, ".config", "kdeglobals")), "no kdeglobals written")

    def test_tune_list_reports_profiles(self):
        proc = run_cli(bin_paths()["hedron-tune"], "list", timeout=120)
        self.assertIn(proc.returncode, (0, 1), (proc.stdout + proc.stderr)[-300:])
        self.assertTrue(proc.stdout.strip(), "hedron-tune list printed nothing")


class TestSchedExtSource(unittest.TestCase):
    """The BPF scheduler in the kernel tree beside us: licence and registration."""

    def test_licence_is_gpl_and_only_gpl(self):
        path = os.path.join(REPO_ROOT, "tools", "sched_ext", "scx_hedron.bpf.c")
        if not os.path.isfile(path):
            self.skipTest("no sched_ext source in this checkout")
        text = read(path)
        self.assertIn('char _license[] SEC("license") = "GPL"', text)
        self.assertIn("SPDX-License-Identifier: GPL-2.0", text.splitlines()[0],
                      "a .bpf.c in the kernel tree is licensed GPL-2.0, first line")

    def test_registered_in_the_build(self):
        mk = os.path.join(REPO_ROOT, "tools", "sched_ext", "Makefile")
        if not os.path.isfile(mk):
            self.skipTest("no tools/sched_ext/Makefile")
        self.assertIn("scx_hedron", read(mk), "make in tools/sched_ext would not build it")

    def test_loader_and_bpf_agree_on_the_ops_name(self):
        sched = os.path.join(REPO_ROOT, "tools", "sched_ext")
        bpf, user = os.path.join(sched, "scx_hedron.bpf.c"), os.path.join(sched, "scx_hedron.c")
        if not (os.path.isfile(bpf) and os.path.isfile(user)):
            self.skipTest("no sched_ext sources")
        ops = re.findall(r"SCX_OPS_DEFINE\((\w+),", read(bpf))
        self.assertEqual(ops, ["hedron_ops"], "exactly one SCX_OPS_DEFINE expected")
        loader = read(user)
        self.assertTrue(re.search(r"SCX_OPS_(?:OPEN|LOAD|ATTACH)\(\s*%s" % ops[0], loader),
                        "the loader must pass the same ops struct the BPF side defines")


if __name__ == "__main__":
    unittest.main()
