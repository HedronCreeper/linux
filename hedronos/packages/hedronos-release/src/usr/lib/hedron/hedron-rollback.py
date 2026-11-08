#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""hedron-rollback — "the update broke my PC" insurance, without needing a new distro.

Real atomic distros (SteamOS, Android, ChromeOS) do A/B or snapshot rollbacks. Arch-based
distros traditionally cannot, and every "just use snapper+grub-btrfs" guide quietly assumes
Btrfs, GRUB and a matching subvolume layout. HedronOS ships something that degrades
honestly instead:

  * on **Btrfs** with a `@`/`@home` layout (our installer default) we take read-only
    subvolume snapshots and add a bootloader entry for the snapshot — a real rollback.
  * on **any other filesystem** (ext4/xfs/btrfs-nonsubvolume) we snapshot the *state that
    actually breaks*: the pacman database, /etc, the module-dependency state, the kernel
    cmdline, the driver plan and the FlowState default. `hedron-rollback restore` puts them
    back and re-runs the driver + initramfs + profile pipelines. Not atomic, but it fixes
    the 95 % of cases which is "a package changed something it should not have".

Every HedronOS transaction (driver apply, profile default change, store install) calls
`snapshot` first, so there is always something to go back to, and the reason string is kept
in the metadata so `list` tells you *why* each snapshot exists.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
# Path bootstrap: the same file must work installed (/usr/lib/…) and straight from a repo
# checkout (packages/<pkg>/src/usr/…) with no install step, so walk up looking for the
# vendored library instead of hard-coding a depth that changes with the packaging layout.
_HERE = os.path.dirname(os.path.abspath(__file__))
for _cand in ("/usr/lib/python3.13/site-packages", os.path.join(_HERE, "..", "python3.13/site-packages")):
    if os.path.isdir(os.path.join(_cand, "hedronlib")) and _cand not in sys.path:
        sys.path.insert(0, os.path.abspath(_cand))
        break
else:
    _d = _HERE
    for _ in range(8):
        _d = os.path.dirname(_d)
        if os.path.isdir(os.path.join(_d, "tools", "hedronlib")):
            sys.path.insert(0, os.path.join(_d, "tools"))
            break

from hedronlib.util import run  # noqa: E402

SNAP_ROOT = os.environ.get("HEDRON_ROLLBACK_DIR", "/var/lib/hedron/rollback")
META = "meta.json"
# Everything is overridable so the test-suite can run the whole snapshot/restore cycle
# against a fixture tree instead of the developer's actual /etc.
ROOT = os.environ.get("HEDRON_ROOT", "/")
DB = os.environ.get("HEDRON_PACMAN_DB", os.path.join(ROOT, "var/lib/pacman"))
PATHS = tuple(os.path.join(ROOT, p) for p in
              ("etc", "var/lib/hedron", "boot/loader/entries", "etc/pacman.conf", "etc/mkinitcpio.conf"))


def tar_compressor() -> str:
    """Pick a compression flag this host's tar actually understands.

    GNU tar gained `--zstd` in 1.31; bsdtar has it too, but some CI images still carry an
    older tar, and a snapshot tool that fails *while you are trying to save you* is worse
    than no tool. So probe once and fall back to xz, which every tar supports.
    """
    probe = run(["tar", "--zstd", "-cf", os.devnull, "-T", os.devnull])
    if probe.returncode == 0:
        return "--zstd"
    return "-J"


def now_id() -> str:
    return time.strftime("%Y%m%d-%H%M%SZ", time.gmtime())


def fs_type(path: str) -> str:
    try:
        with open("/proc/mounts", encoding="utf-8") as fh:
            best, best_len = "", 0
            for line in fh:
                parts = line.split()
                if len(parts) < 3:
                    continue
                mount, fstype = parts[1], parts[2]
                if path.startswith(mount) and len(mount) > best_len:
                    best, best_len = fstype, len(mount)
            return best or "unknown"
    except OSError:
        return "unknown"


def btrfs_subvolume(root: str) -> bool:
    proc = run(["btrfs", "subvolume", "show", root])
    return proc.returncode == 0


def list_snaps(args: argparse.Namespace) -> int:
    if not os.path.isdir(SNAP_ROOT):
        print("no snapshots yet (%s does not exist)" % SNAP_ROOT)
        return 0
    rows = []
    for name in sorted(os.listdir(SNAP_ROOT), reverse=True):
        meta_path = os.path.join(SNAP_ROOT, name, META)
        meta = {}
        if os.path.isfile(meta_path):
            try:
                meta = json.load(open(meta_path, encoding="utf-8"))
            except json.JSONDecodeError:
                meta = {"reason": "(metadata unreadable)"}
        elif os.path.isdir(os.path.join(SNAP_ROOT, name)):
            meta = {"reason": "bootloader snapshot", "kind": "subvolume"}
        size = 0
        for ext in ("zst", "xz"):
            target = os.path.join(SNAP_ROOT, name, "state.tar." + ext)
            if os.path.isfile(target):
                size = os.path.getsize(target)
                break
        rows.append([name, meta.get("kind", "state"), meta.get("reason", "")[:52],
                     "%.1f MiB" % (size / 1048576.0) if size else "-",
                     str(meta.get("kernel", ""))[:18], "yes" if meta.get("boot_entry") else "-"])
    if not rows:
        print("no snapshots yet")
        return 0
    print("%-20s %-10s %-52s %-10s %-18s %s" % ("id", "kind", "reason", "size", "kernel", "boot entry"))
    for row in rows:
        print("%-20s %-10s %-52s %-10s %-18s %s" % tuple(row))
    if args.json:
        print(json.dumps(rows, indent=1))
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    reason = args.reason or "manual"
    snap_id = args.id or "%s-%s" % (now_id(), reason.split(":", 1)[0])
    os.makedirs(SNAP_ROOT, exist_ok=True)
    dest = os.path.join(SNAP_ROOT, snap_id)
    if os.path.exists(dest):
        print("snapshot %s already exists" % snap_id, file=sys.stderr)
        return 2
    os.makedirs(dest)
    kind = "state"
    boot_entry = False

    root = "/"
    if fs_type(root) == "btrfs" and btrfs_subvolume(root):
        proc = run(["btrfs", "subvolume", "snapshot", "-r", root,
                    os.path.join(dest, "root.snap")])
        if proc.returncode == 0:
            kind = "subvolume"
    present = [p for p in PATHS if os.path.exists(p)]
    if os.path.isdir(DB):
        present.append(DB)
    compressor = tar_compressor()
    archive = os.path.join(dest, "state.tar." + ("zst" if compressor == "zstd" else "xz"))
    tar = ["tar", "-c", "--acls", "--xattrs", "--selinux", "--exclude", dest,
           compressor, "-f", archive] + [p for p in present]
    proc = run(tar)
    if proc.returncode != 0:
        print("hedron-rollback: state archive failed:\n%s" % proc.stderr[-1200:], file=sys.stderr)
        return 1
    meta = {"id": snap_id, "reason": reason, "when": time.time(), "kind": kind,
            "kernel": run(["uname", "-r"]).stdout.strip(),
            "cmdline": open("/proc/cmdline", encoding="utf-8").read().strip(),
            "paths": present, "compressor": "zstd" if compressor == "--zstd" else "xz", "boot_entry": boot_entry,
            "size": os.path.getsize(archive)}
    with open(os.path.join(dest, META), "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=1, sort_keys=True)
    if args.boot_entry and kind == "subvolume":
        boot_entry = write_boot_entry(snap_id, dest)
        meta["boot_entry"] = boot_entry
        with open(os.path.join(dest, META), "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=1, sort_keys=True)
    print("snapshot %s (%s, %.1f MiB%s)" % (snap_id, kind, meta["size"] / 1048576.0,
                                            ", bootloader entry added" if boot_entry else ""))
    prune(args.keep)
    return 0


def write_boot_entry(snap_id: str, dest: str) -> bool:
    """Add a systemd-boot entry that mounts the read-only snapshot (real rollback boot)."""
    entries = "/boot/loader/entries"
    if not os.path.isdir(entries):
        return False
    body = ("title HedronOS snapshot %s (read-only rollback boot)\n"
            "linux /@/boot/vmlinuz-linux-hedron\n"
            "initrd /@/boot/initramfs-linux-hedron.img\n"
            "options root=UUID=%s ro ro.snaps=%s rw init=/usr/lib/hedron/hedron-rollback.py --early-restore %s\n"
            % (snap_id, root_uuid(), snap_id, snap_id))
    with open(os.path.join(entries, "99-hedron-snapshot-%s.conf" % snap_id), "w", encoding="utf-8") as fh:
        fh.write(body)
    return True


def root_uuid() -> str:
    proc = run(["blkid", "-s", "UUID", "-o", "value", "/"])
    out = proc.stdout.strip()
    if out:
        return out
    proc = run(["findmnt", "-n", "-o", "UUID", "/"])
    return proc.stdout.strip() or "unknown"


def prune(keep: int) -> None:
    if keep <= 0:
        return
    names = sorted(n for n in os.listdir(SNAP_ROOT) if os.path.isdir(os.path.join(SNAP_ROOT, n)))
    for name in names[:-keep]:
        shutil.rmtree(os.path.join(SNAP_ROOT, name), ignore_errors=True)


def cmd_restore(args: argparse.Namespace) -> int:
    snap_id = args.id or latest()
    if not snap_id:
        print("nothing to restore from", file=sys.stderr)
        return 2
    dest = os.path.join(SNAP_ROOT, snap_id)
    meta_path = os.path.join(dest, META)
    if not os.path.isfile(meta_path):
        print("%s has no metadata; refusing to guess" % snap_id, file=sys.stderr)
        return 2
    meta = json.load(open(meta_path, encoding="utf-8"))
    archive = os.path.join(dest, "state." + ("tar.zst" if meta.get("compressor") == "zstd" else "tar.xz"))
    if not os.path.isfile(archive) and meta["kind"] != "subvolume":
        print("archive %s is missing" % archive, file=sys.stderr)
        return 2
    if not args.yes:
        print("This will overwrite %s from snapshot %s and rebuild the initramfs." % (", ".join(meta["paths"]), snap_id))
        print("Re-run with --yes if you mean it (or `hedron-rollback boot %s` to boot the snapshot instead)." % snap_id)
        return 3
    if args.dry_run:
        print("would extract %s → /" % archive)
        print("would then re-run: hedron-drivers apply --from-snapshot %s && hedron-tune apply --from-default boot && mkinitcpio -P" % snap_id)
        return 0
    cmd = ["tar", "--acls", "--xattrs", "--selinux",
           "--zstd" if meta.get("compressor") == "zstd" else "-J",
           "-xf", archive, "-C", ROOT]
    proc = run(cmd)
    if proc.returncode != 0:
        print("extract failed:\n%s" % proc.stderr[-1500:], file=sys.stderr)
        return 1
    print("state restored from %s; re-running the pipelines that depend on it" % snap_id)
    for step in (["hedron-drivers", "apply", "--from-snapshot", snap_id],
                 ["hedron-tune", "profile", "set", default_profile(meta)],
                 ["mkinitcpio", "-P"]):
        if not shutil.which(step[0]):
            print("  (skipping %s: not installed)" % step[0])
            continue
        proc = run(step)
        print("  %s → %s" % (shlex.join(step), "ok" if proc.returncode == 0 else "FAILED"))
        if proc.returncode != 0:
            print(proc.stderr[-800:], file=sys.stderr)
    print("\nDone. Reboot to make the kernel-side changes take effect.")
    return 0


def default_profile(meta: dict) -> str:
    snap_dir = os.path.join(SNAP_ROOT, meta.get("id", ""))
    flow = next((os.path.join(snap_dir, "state.tar." + e) for e in ("zst", "xz")
                 if os.path.isfile(os.path.join(snap_dir, "state.tar." + e))), "")
    probe = run(["tar", "--zstd" if flow.endswith(".zst") else "-J", "-xOf", flow,
                "var/lib/hedron/flowstate/default"]) if flow and os.path.isfile(flow) else None
    if probe and probe.stdout.strip():
        return probe.stdout.strip()
    return "balanced"


def latest() -> str | None:
    if not os.path.isdir(SNAP_ROOT):
        return None
    names = sorted(n for n in os.listdir(SNAP_ROOT) if os.path.isdir(os.path.join(SNAP_ROOT, n)))
    return names[-1] if names else None


def cmd_boot(args: argparse.Namespace) -> int:
    snap_id = args.id or latest()
    if not snap_id:
        print("no snapshots to boot", file=sys.stderr)
        return 2
    ok = write_boot_entry(snap_id, os.path.join(SNAP_ROOT, snap_id))
    if not ok:
        print("no bootloader entries directory found (need /boot/loader/entries); "
              "on GRUB use `hedron-rollback restore %s --yes` instead" % snap_id, file=sys.stderr)
        return 1
    print("added a bootloader entry for %s — pick it at the boot menu (5 s timeout)." % snap_id)
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    snap_id = args.id or latest()
    if not snap_id:
        print("no snapshots")
        return 1
    meta = json.load(open(os.path.join(SNAP_ROOT, snap_id, META), encoding="utf-8"))
    print(json.dumps(meta, indent=1, sort_keys=True))
    return 0


def cmd_gc(args: argparse.Namespace) -> int:
    before = time.time() - args.age_days * 86400
    removed = 0
    for name in sorted(os.listdir(SNAP_ROOT)) if os.path.isdir(SNAP_ROOT) else []:
        path = os.path.join(SNAP_ROOT, name)
        if not os.path.isdir(path):
            continue
        try:
            meta = json.load(open(os.path.join(path, META), encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if meta.get("when", 0) < before and not meta.get("pinned"):
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
    print("removed %d snapshot(s) older than %d days" % (removed, args.age_days))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="hedron-rollback", description=__doc__.splitlines()[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("snapshot", help="capture the current state")
    p.add_argument("--reason", default="")
    p.add_argument("--id", default=None)
    p.add_argument("--boot-entry", action="store_true")
    p.add_argument("--keep", type=int, default=8)
    p.set_defaults(func=cmd_snapshot)
    p = sub.add_parser("list", parents=[])
    p.set_defaults(func=list_snaps)
    p = sub.add_parser("restore", help="put the state back")
    p.add_argument("id", nargs="?", default=None)
    p.add_argument("--yes", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(func=cmd_restore)
    p = sub.add_parser("boot", help="add a bootloader entry for a snapshot")
    p.add_argument("id", nargs="?", default=None)
    p.set_defaults(func=cmd_boot)
    p = sub.add_parser("info")
    p.add_argument("id", nargs="?", default=None)
    p.set_defaults(func=cmd_info)
    p = sub.add_parser("gc")
    p.add_argument("--age-days", type=int, default=14)
    p.set_defaults(func=cmd_gc)
    p = sub.add_parser("status", help="is rollback available on this filesystem?")
    p.set_defaults(func=lambda a: print(json.dumps({
        "root_fs": fs_type("/"), "btrfs_subvolume": btrfs_subvolume("/"),
        "mode": "subvolume+boot-entry" if (fs_type("/") == "btrfs" and btrfs_subvolume("/")) else "state-archive",
        "snapshots": len(os.listdir(SNAP_ROOT)) if os.path.isdir(SNAP_ROOT) else 0,
        "store": SNAP_ROOT}, indent=1, sort_keys=True)) or 0)

    args = ap.parse_args(argv)
    if args.cmd is None:
        return list_snaps(argparse.Namespace(json=False))
    try:
        return args.func(args)
    except AttributeError as exc:
        print("hedron-rollback: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
