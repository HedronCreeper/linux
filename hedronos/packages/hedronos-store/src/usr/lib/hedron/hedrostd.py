#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""hedrostd — the store's transaction server.

Why a daemon for an app store at all, when `hstore` could just exec pacman? Because a
cross-backend install is the part that must not be interrupted or duplicated:

  * one index snapshot per transaction, so a repo refresh halfway through cannot make you
    install `foo 1.2` and `bar 0.9` from incompatible states;
  * a journal (/var/lib/hedron/store/journal.jsonl) that records every attempt, so `hstore
    undo` works even after a power cut, and `hedron-doctor` can tell you what the machine
    did at 03:12;
  * serialisation: two windows (or the GUI and a terminal) must not both run `pacman -Syu`.

The daemon is deliberately *dumb*: it decides ordering, records, and hands each step to the
backend CLI (`pacman`, `flatpak`, AppImage placement, AUR helper). It never parses package
format itself, and it never talks to the network except through those tools. That keeps the
attack surface in `pacman` (audited for twenty years) instead of in us.

Socket protocol: JSON lines, one request per line, one reply per request.
  {"cmd":"search","q":"launcher"}            → {"ok":true,"results":[…]}
  {"cmd":"plan","install":["steam"],…}        → {"ok":true,"steps":[…]}
  {"cmd":"apply","token":"…"}                 → {"ok":true,"journal":[…]}
  {"cmd":"status"} / {"cmd":"explain"} / {"cmd":"cancel","token":"…"}
"""

from __future__ import annotations

import argparse
import json
import os
import select
import shutil
import signal
import socket
import subprocess
import sys
import time
import uuid

_HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in ("/usr/lib/python3.13/site-packages", os.path.join(_HERE, "..", "python3.13/site-packages")):
    if os.path.isdir(os.path.join(_cand, "hedronlib")) and _cand not in sys.path:
        sys.path.insert(0, os.path.abspath(_cand))
        break
_d = _HERE
for _ in range(8):
    _d = os.path.dirname(_d)
    if os.path.isdir(os.path.join(_d, "tools", "hedronlib")):
        sys.path.insert(0, os.path.join(_d, "tools"))
        break

from hedronlib import storeindex  # noqa: E402
from hedronlib.util import load_toml  # noqa: E402

SOCK = os.environ.get("HEDRON_STORE_SOCK") or "/run/hedron/store/sock"
STATE = os.environ.get("HEDRON_STORE_STATE") or "/var/lib/hedron/store"
JOURNAL = os.path.join(STATE, "journal.jsonl")
CURRENT = os.path.join(STATE, "current.json")
CONF = os.environ.get("HEDRON_STORE_CONF") or "/etc/hedron/store.toml"
IDLE_EXIT = 0


def log(msg: str) -> None:
    # stderr, always. `hstore status --json` imports this module to answer a request when the daemon
    # is not running, so a log line on stdout would be prepended to the JSON document and break every
    # consumer that pipes hstore into jq.
    print("%s hedrostd: %s" % (time.strftime("%H:%M:%S"), msg), file=sys.stderr, flush=True)


def conf() -> dict:
    out = {"index": os.path.join(STATE, "index.json"), "version": "2026.11.07",
           "mirror": "https://mirror.hedronos.local/hedron-os",
           "auto_refresh_hours": 24, "allow_unverified": False, "confirm_removals": True,
           "flatpak_remote": "flathub", "aur_helper": "aurman", "offline_ok": True,
           "rollback_snapshots": True}
    if os.path.isfile(CONF):
        try:
            data = load_toml(CONF)
            out.update(data.get("store", {}) or {})
        except Exception as exc:  # noqa: BLE001
            log("ignoring %s: %s" % (CONF, exc))
    return out


class Store:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.index: storeindex.Index | None = None
        self.pending: dict[str, dict] = {}
        self.load_index()

    # ------------------------------------------------------------------ index
    def load_index(self) -> None:
        path = self.cfg["index"]
        if not os.path.isfile(path):
            log("no index at %s yet (`hstore update` builds it)" % path)
            self.index = None
            return
        try:
            self.index = storeindex.load(path, verify_signature=not self.cfg["allow_unverified"])
        except storeindex.IndexError_ as exc:
            log("index refused: %s" % exc)
            self.index = None
            return
        log("index: %d entries (%s)" % (len(self.index.entries), self.index.meta.get("verification", "unsigned")))

    def refresh(self, *, force: bool = False) -> dict:
        """Fetch + verify the index. Uses the ISO's local cache when offline.

        We deliberately do NOT implement HTTP: `curl` with the mirror list, then verify the
        signature. A store that reimplements transport gets certificate errors on someone's
        captive-portal network and bricks the app centre.
        """
        cfg = self.cfg
        url = "%s/hedron-store-index.json" % cfg["mirror"].rstrip("/")
        tmp = cfg["index"] + ".part"
        os.makedirs(os.path.dirname(cfg["index"]), exist_ok=True)
        age = 0
        if os.path.isfile(cfg["index"]):
            age = time.time() - os.path.getmtime(cfg["index"])
        if not force and age < cfg["auto_refresh_hours"] * 3600:
            return {"ok": True, "refreshed": False, "age_h": round(age / 3600.0, 1),
                    "next_in_h": round((cfg["auto_refresh_hours"] * 3600 - age) / 3600.0, 1)}
        rc, why = -1, "not attempted"
        if shutil.which("curl"):
            proc = subprocess.run(["curl", "-fsSL", "--connect-timeout", "8", "--max-time", "60",
                                   "-o", tmp, url + ".sig", "-w", "%{http_code}"], capture_output=True, text=True)
            if proc.returncode == 0 and os.path.isfile(tmp):
                sig = subprocess.run(["curl", "-fsSL", "--max-time", "20", "-o", tmp + ".sig",
                                      url + ".sig"], capture_output=True, text=True)
                os.replace(tmp, cfg["index"])
                if sig.returncode == 0:
                    os.replace(tmp + ".sig", cfg["index"] + ".sig")
                rc, why = 0, "fetched with curl"
        if rc != 0 and os.path.isdir("/var/cache/hedron/store"):
            cached = "/var/cache/hedron/store/index.json"
            if os.path.isfile(cached):
                shutil.copy2(cached, cfg["index"])
                if os.path.isfile(cached + ".sig"):
                    shutil.copy2(cached + ".sig", cfg["index"] + ".sig")
                rc, why = 0, "installed from the on-ISO cache (offline)"
        if rc != 0:
            return {"ok": False, "refreshed": False, "reason": why, "url": url}
        try:
            self.index = storeindex.load(cfg["index"], verify_signature=not cfg["allow_unverified"])
        except storeindex.IndexError_ as exc:
            return {"ok": False, "refreshed": False, "reason": "downloaded index rejected: %s" % exc,
                    "url": url}
        return {"ok": True, "refreshed": True, "entries": len(self.index.entries),
                "verification": self.index.meta.get("verification", "?")}

    # ------------------------------------------------------------------ plan
    def plan(self, *, install: list[str], remove: list[str], update_all: bool = False) -> dict:
        if self.index is None:
            return {"ok": False, "reason": "no index (hstore update)"}
        steps: list[dict] = []
        unknown: list[str] = []
        for app_id in install:
            entry = self.index.by_id(app_id)
            if entry is None:
                unknown.append(app_id)
                continue
            steps.append({"op": "install", "app": entry.id, "source": entry.source,
                          "provider": entry.provider, "all_providers": providers_of(entry),
                          "size_mib": entry.size_mib, "tier": entry.tier, "post": entry.install})
        for app_id in remove:
            entry = self.index.by_id(app_id)
            if entry is None:
                unknown.append(app_id)
                continue
            steps.append({"op": "remove", "app": entry.id, "source": entry.source,
                          "provider": entry.provider, "confirm": bool(self.cfg["confirm_removals"])})
        if update_all:
            steps.append({"op": "update", "source": "pacman", "provider": "-Syu"})
            steps.append({"op": "update", "source": "flatpak", "provider": "update"})
            if any(e.source == "aur" for e in self.index.entries):
                steps.append({"op": "update", "source": "aur", "provider": self.cfg["aur_helper"]})
        blocked = [s for s in steps if s["op"] == "install" and s.get("tier") == "unsupported"
                   and not self.cfg["allow_unverified"]]
        return {"ok": not unknown, "steps": steps, "unknown": unknown, "blocked": [s["app"] for s in blocked],
                "total_mib": round(sum(s.get("size_mib", 0) for s in steps if s["op"] == "install"), 1),
                "warnings": [] if not blocked else ["%d unsupported-tier app(s) will not install without "
                                                     "--allow-unverified" % len(blocked)]}

    # ------------------------------------------------------------------ apply
    def apply(self, steps: list[dict], *, dry_run: bool = False) -> dict:
        token = uuid.uuid4().hex[:12]
        started = time.time()
        results = []
        self.journal({"event": "begin", "token": token, "steps": steps, "dry_run": dry_run})
        for step in steps:
            cmd = command_for(step, self.cfg)
            entry = {"op": step["op"], "source": step["source"], "provider": step.get("provider"),
                     "app": step.get("app"), "cmd": cmd}
            if dry_run or not cmd:
                entry.update({"rc": None, "note": "dry run" if dry_run else "no command for this source"})
                results.append(entry)
                continue
            proc = subprocess.run(cmd, capture_output=True, text=True)
            entry.update({"rc": proc.returncode,
                          "tail": ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip().splitlines()[-3:]})
            results.append(entry)
            self.journal({"event": "step", "token": token, **entry})
            if proc.returncode != 0:
                self.journal({"event": "abort", "token": token, "failed": entry})
                return {"ok": False, "token": token, "steps": results,
                        "reason": "%s failed (%s)" % (step["op"], proc.returncode),
                        "hint": "hstore undo %s reverts what already ran" % token}
        for step in steps:
            if step["op"] == "install" and step.get("post") and not dry_run:
                for hook in step["post"]:
                    proc = subprocess.run(hook, shell=True, capture_output=True, text=True)  # noqa: S602
                    self.journal({"event": "post-install", "token": token, "app": step["app"],
                                  "cmd": hook, "rc": proc.returncode})
        duration = round(time.time() - started, 2)
        self.journal({"event": "end", "token": token, "ok": True, "seconds": duration})
        if self.cfg["rollback_snapshots"] and not dry_run:
            subprocess.run(["hedron-rollback", "snapshot", "--reason", "store:" + token],
                           capture_output=True, text=True)
        return {"ok": True, "token": token, "steps": results, "seconds": duration}

    def undo(self, token: str | None) -> dict:
        events = []
        if not os.path.isfile(JOURNAL):
            return {"ok": False, "reason": "no journal"}
        with open(JOURNAL, encoding="utf-8") as fh:
            for line in fh:
                try:
                    doc = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if token is None or doc.get("token") == token:
                    events.append(doc)
        if not events:
            return {"ok": False, "reason": "no transaction %s in the journal" % token}
        if token is None:
            tokens = [e.get("token") for e in events if e.get("event") == "begin"]
            token = tokens[-1] if tokens else None
            if not token:
                return {"ok": False, "reason": "nothing recorded to undo"}
            events = [e for e in events if e.get("token") == token]
        inverse = []
        for event in reversed(events):
            if event.get("event") != "step" or event.get("rc") not in (0, None):
                continue
            flip = {"install": "remove", "remove": "install", "update": "update"}[event["op"]]
            inverse.append({"op": flip, "source": event["source"], "provider": event["provider"],
                            "app": event.get("app"), "undo_of": token})
        if not inverse:
            return {"ok": False, "reason": "transaction %s has no reversible steps" % token}
        return {"ok": True, "token": token, "steps": inverse}

    def journal(self, doc: dict) -> None:
        try:
            os.makedirs(os.path.dirname(JOURNAL), exist_ok=True)
            doc.setdefault("when", time.time())
            with open(JOURNAL, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(doc, sort_keys=True) + "\n")
        except OSError:
            pass

    # ------------------------------------------------------------------ queries
    def search(self, text: str, **kw) -> dict:
        if self.index is None:
            return {"ok": False, "reason": "no index (hstore seed / hstore update --index)"}
        kwargs = {k: v for k, v in kw.items() if v and k in ("category", "source", "tier")}
        entries = self.index.search(text, **kwargs)
        installed = _installed_ids()
        results = []
        for entry in entries:
            doc = entry.as_dict()
            if entry.id in installed:
                doc["flags"] = sorted(set(doc.get("flags", []) + ["installed"]))
            results.append(doc)
        return {"ok": True, "results": results, "total": len(entries)}

    def status(self) -> dict:
        doc = {"index": self.cfg["index"], "entries": len(self.index.entries) if self.index else 0,
               "verified": self.index.verified if self.index else None,
               "verification": (self.index.meta.get("verification", "no index") if self.index else "no index"),
               "mirror": self.cfg["mirror"], "socket": SOCK, "pending": list(self.pending),
               "journal_lines": sum(1 for _ in open(JOURNAL, encoding="utf-8")) if os.path.isfile(JOURNAL) else 0}
        if self.index:
            doc["stats"] = self.index.stats()
        return doc


def _installed_ids() -> set[str]:
    """Best-effort 'what is already here', from the tools that own that knowledge.

    Cached for the life of the process: a search that shells out to `flatpak list` per keystroke
    is how software centres feel slow.
    """
    global _INSTALLED_CACHE
    if _INSTALLED_CACHE is not None:
        return _INSTALLED_CACHE
    found: set[str] = set()
    if shutil.which("pacman"):
        proc = subprocess.run(["pacman", "-Qqe"], capture_output=True, text=True)
        if proc.returncode == 0:
            found |= {line.split()[0] for line in proc.stdout.splitlines() if line.strip()}
    if shutil.which("flatpak"):
        proc = subprocess.run(["flatpak", "list", "--app", "--columns=application"], capture_output=True,
                              text=True)
        if proc.returncode == 0:
            found |= {line.strip() for line in proc.stdout.splitlines() if line.strip()}
    _INSTALLED_CACHE = found
    return found


_INSTALLED_CACHE: set[str] | None = None


def providers_of(entry: storeindex.Entry) -> list[str]:
    return [entry.provider] if entry.provider else []


def command_for(step: dict, cfg: dict) -> list[str]:
    """Translate a planned step into the backend's own command line."""
    op, source, provider = step["op"], step.get("source"), step.get("provider")
    if source == "pacman":
        if op == "install":
            return ["/usr/bin/pacman", "-S", "--noconfirm", "--needed", provider] if shutil.which("pacman") else []
        if op == "remove":
            return ["/usr/bin/pacman", "-Rns", "--noconfirm", provider] if shutil.which("pacman") else []
        if op == "update":
            return ["/usr/bin/pacman", "-Syu", "--noconfirm"] if shutil.which("pacman") else []
    if source == "hedron":
        # HedronOS-native packages live in our own repo; installing them is a pacman
        # transaction against a different database so we can pin the repo in one place.
        return ["/usr/bin/pacman", "-S", "--noconfirm", provider] if shutil.which("pacman") else []
    if source == "flatpak":
        if op == "install":
            return ["/usr/bin/flatpak", "install", "-y", cfg["flatpak_remote"], provider] if shutil.which("flatpak") else []
        if op == "remove":
            return ["/usr/bin/flatpak", "uninstall", "-y", provider] if shutil.which("flatpak") else []
        if op == "update":
            return ["/usr/bin/flatpak", "update", "--assumeyes"] if shutil.which("flatpak") else []
    if source == "aur":
        helper = shutil.which(cfg["aur_helper"]) or shutil.which("yay") or shutil.which("paru")
        return [helper, "-S", "--noconfirm", "--needed", provider] if helper else []
    if source == "appimage":
        target = os.path.expanduser("~/Applications/%s.AppImage" % step.get("app", "app"))
        if op == "install":
            return ["/usr/bin/sh", "-c", "mkdir -p ~/Applications && curl -fsSL %s -o %s && chmod +x %s"
                    % (provider, target, target)]
        if op == "remove":
            return ["/usr/bin/rm", "-f", target]
    return []


# ------------------------------------------------------------------- serve/once
def handle_line(store: Store, line: str) -> dict:
    try:
        req = json.loads(line)
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": "invalid JSON: %s" % exc}
    cmd = req.get("cmd", "")
    if cmd == "ping":
        return {"ok": True, "pong": True, "entries": len(store.index.entries) if store.index else 0}
    if cmd == "status":
        return {"ok": True, **store.status()}
    if cmd == "search":
        reply = store.search(req.get("q", ""), category=req.get("category"), source=req.get("source"),
                             tier=req.get("tier"))
        limit = int(req.get("limit", 60) or 60)
        reply["results"] = reply.get("results", [])[:limit]
        return reply
    if cmd == "info":
        if store.index is None:
            return {"ok": False, "error": "no index"}
        entry = store.index.by_id(req.get("app", ""))
        return {"ok": bool(entry), "entry": entry.as_dict() if entry else None}
    if cmd == "plan":
        plan = store.plan(install=req.get("install", []) or [], remove=req.get("remove", []) or [],
                          update_all=bool(req.get("update_all")))
        if plan["ok"]:
            token = uuid.uuid4().hex[:12]
            store.pending[token] = plan
            plan["token"] = token
            plan["expires_in_s"] = 300
        return plan
    if cmd == "apply":
        token = req.get("token", "")
        plan = store.pending.pop(token, None)
        if plan is None:
            if not plan and req.get("install") or req.get("remove"):
                plan = store.plan(install=req.get("install", []) or [], remove=req.get("remove", []) or [],
                                  update_all=bool(req.get("update_all")))
            else:
                return {"ok": False, "error": "unknown or expired token (run `hstore plan` again)"}
        return store.apply(plan.get("steps", []), dry_run=bool(req.get("dry_run")))
    if cmd == "undo":
        undo = store.undo(req.get("token"))
        if undo.get("ok") and req.get("apply"):
            return {**store.apply(undo["steps"], dry_run=bool(req.get("dry_run"))), "undo_of": undo["token"]}
        return undo
    if cmd == "refresh":
        return store.refresh(force=bool(req.get("force")))
    if cmd == "explain":
        return {"ok": True, "explain": {"state_dir": STATE, "socket": SOCK, "conf": CONF,
                                        "config": store.cfg,
                                        "how": "hedrostd plans across backends and applies through them; "
                                               "it never parses packages or talks to the network itself"}}
    if cmd == "journal":
        lines = []
        if os.path.isfile(JOURNAL):
            with open(JOURNAL, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        lines.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        return {"ok": True, "events": lines[-int(req.get("limit", 50)):]}
    return {"ok": False, "error": "unknown cmd %r" % cmd}


def serve(store: Store, *, one_shot: bool = False) -> int:
    os.makedirs(os.path.dirname(SOCK), exist_ok=True)
    if os.path.exists(SOCK):
        os.unlink(SOCK)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(SOCK)
    listener.listen(16)
    os.chmod(SOCK, 0o660)
    log("listening on %s (state %s)" % (SOCK, STATE))
    stop = {"v": False}
    signal.signal(signal.SIGTERM, lambda *_a: stop.update(v=True))
    idle_after = float(os.environ.get("HEDRON_STORE_IDLE_EXIT", "0"))
    last_activity = time.time()
    while not stop["v"]:
        r, _, _ = select.select([listener], [], [], 1.0)
        if not r:
            if idle_after and time.time() - last_activity > idle_after:
                log("idle for %d s, exiting (socket-activated services come back on demand)" % idle_after)
                break
            continue
        last_activity = time.time()
        conn, _ = listener.accept()
        with conn:
            conn.settimeout(120)
            data = b""
            while b"\n" not in data:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
            for line in data.decode("utf-8", "replace").splitlines():
                if not line.strip():
                    continue
                reply = handle_line(store, line)
                try:
                    conn.sendall((json.dumps(reply, sort_keys=True) + "\n").encode())
                except OSError:
                    pass
        if one_shot:
            break
    listener.close()
    try:
        os.unlink(SOCK)
    except OSError:
        pass
    return 0


def request(payload: dict, timeout: float = 15.0) -> dict:
    """Client side, used by `hstore` and the QML GUI's `--json` fallback."""
    if not os.path.exists(SOCK):
        return {"ok": False, "error": "hedrostd is not running (systemctl --user start hedrostd)"}
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(SOCK)
    s.sendall((json.dumps(payload) + "\n").encode())
    chunks = []
    while True:
        data = s.recv(65536)
        if not data:
            break
        chunks.append(data)
        if b"\n" in data:
            break
    s.close()
    raw = b"".join(chunks).decode("utf-8", "replace").strip()
    try:
        return json.loads(raw.splitlines()[-1]) if raw else {"ok": False, "error": "empty reply"}
    except json.JSONDecodeError:
        return {"ok": False, "error": "bad reply: %s" % raw[:200]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hedrostd", description=__doc__.splitlines()[0])
    ap.add_argument("--serve", action="store_true", help="run the socket server (default)")
    ap.add_argument("--once", action="store_true", help="start, refresh index if needed, exit")
    ap.add_argument("--request", default=None, help="send one JSON line to a running daemon and print the reply")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    store = Store(conf())
    if args.request:
        print(json.dumps(handle_line(store, args.request), indent=1, sort_keys=True))
        return 0
    if args.status:
        print(json.dumps(store.status(), indent=1, sort_keys=True))
        return 0
    if args.once:
        store.refresh()
        return 0
    return serve(store, one_shot=False)


if __name__ == "__main__":
    raise SystemExit(main())
