# HedronOS — the game-first general-purpose distro

> **HedronOS 1.0 "Halcyon"** — an Arch-rolling, Wayland-native, gaming-first Linux
> distribution whose defining trick is that it *looks at your hardware before it
> installs anything*: the GPU in your machine is detected in the live session, the
> correct vendor stack (NVIDIA open/proprietary/legacy, AMDGPU + RADV, Intel Xe/KMD +
> ANV) is resolved, pre-seeded on the ISO, and installed with the exact kernel
> parameters, initramfs modules and firmware that family needs. No `nomodeset`
> forums, no "please install drivers after reboot".

This directory is the **entire distro source**: build system, rootfs overlays, native
packages, curated app catalog, driver rules, the Hedron kernel configuration, and the
in-tree `scx_hedron` BPF scheduler that ships in `../tools/sched_ext/`.

There is **no server, no CDN and no CI artifact** in the loop. You clone, you run one
script, you get a signed `.iso`.

```
./build.sh --profile gamerte            # → dist/hedronos-1.0.0-gamerte-x86_64.iso (+ .sig, .sha256)
```

---

## Table of contents

| Doc | What is in it |
| --- | --- |
| [`docs/FEATURES.md`](docs/FEATURES.md) | every HedronOS-unique feature, and why it beats Fedora Gamer/ Nobara/ Bazzite/ Windows |
| [`docs/02-building.md`](docs/02-building.md) | building the ISO on any host (Arch, Debian, Fedora, macOS-ish), offline builds, reproducible builds |
| [`docs/03-hardware-and-drivers.md`](docs/03-hardware-and-drivers.md) | the GPU detection/decision engine, the PCI-id database generated from this kernel tree |
| [`docs/04-hedron-store.md`](docs:04-hedron-store.md) | Hedron Store: index format, signing, daemon protocol, curated catalog |
| [`docs/05-performance-notes.md`](docs/05-performance-notes.md) | `scx_hedron`, FlowState tuning, frame-pacing numbers we chase |
| [`docs/06-install-manual.md`](docs/06-install-manual.md) | the pre-install app picker, partitioning, first boot |
| [`docs/07-handheld.md`](docs/07-handheld.md) | Legion Go / ROG Ally / Steam Deck path |
| [`docs/08-kernel-notes.md`](docs/08-kernel-notes.md) | what we changed in `../kernel` and why |
| [`tests/README.md`](tests/README.md) | how the "verify everything" gate works |

---

## What you get

**Gaming**

* **Auto-provisioned GPU stack.** `hedron-gpu-detect` parses PCI IDs, DMI and the
  kernel's own module alias tables; `hedron-drivers` installs the matching vendor
  stack, writes `modprobe.d`/`initcpio` config, and pre-baked NVIDIA DKMS modules are
  compiled against *this* tree's headers at ISO build time. Offline install still gets
  the right driver.
* **`scx_hedron`** — an in-tree `sched_ext` BPF scheduler (see
  [`../tools/sched_ext/scx_hedron.bpf.c`](../tools/sched_ext/scx_hedron.bpf.c)). The
  foreground game gets a dedicated, priority-slice DSQ, its render thread gets
  `SCX_SLICE_DFL*4`, compositor + audio threads get an "untouchable" band so you stop
  shipping 0.1% low spikes to your players.
* **FlowState** (`hedronflow`): one transactional profile engine that flips CPU
  governor/EPP, idle states, GPU power profile, swap, THP, IRQ affinity and RT
  limits as one atomic, auditable, revertible change.
* **GameMode++** (`hgm`): the classic LD_PRELOAD socket protocol, plus cgroup scopes
  (`hedron-run`), Steam pre-launch hooks, per-title profiles, and an *auto* mode that
  engages the moment a Vulkan/DXVK fullscreen surface appears.
* **Instant Replay**: `hedron-clipd` keeps an NVENC/AMF/VAAPI rolling ring buffer
  (60 s) in userspace; one hotkey writes a scrubbed MP4. No Discord, no OBS daemon
  eating a core while you play.
* **Handheld mode**: `hedronos-handheld` gives TDP/power-cap control, controller-as-
  mouse, gamescope session, and a resume-safe GPU clock lock.

**Every day**

* **Hedron Store** — pacman + Flatpak + AppImage + AUR behind one signed index, one
  progress state machine, one CLI (`hstore`) and one GUI (libadwaita when present,
  ANSI TUI always). "Verified" tier, per-app sandbox toggles, one-click *update
  everything without touching my AUR mess*.
* **`hedron-firstsetup`** — the pre-install app picker (Steam pre-selected and
  pre-cached), a first-boot wizard, and `hedron-rollback` snapshotting `/` with
  btrfs send/receive so any driver or update can be un-done from the boot menu.
* **Daily niceties**: `hedron-why` ("why is my fan screaming?"), `hedron-doctor`
  (read-only health report + copy-paste bug bundle), `hedron-snap` (region capture),
  `hedron-pulse` (monitor VRR/HDR/DDC controls), `hedron-vitals` (OSD).

---

## Layout

```
hedronos/
├── build.sh            # THE script you run: deps → kernel → packages → rootfs → ISO → sign → verify
├── Makefile            # make iso | kernel | packages | verify | test | vm | clean | release
├── build/              # build engine (bash lib + profiles)
├── airootfs/           # overlay copied into the LIVE rootfs (the desktop you boot into)
├── overlay/            # overlay applied to the INSTALLED system
├── packages/           # 11 HedronOS-native packages (PKGBUILD + real sources)
├── data/               # app catalog, GPU db, driver rules, package sets, mirrors
├── kernel/             # hedronos_defconfig, config fragments, linux-hedron PKGBUILD
├── tools/              # hedronos-verify, gen-gpu-db.py, mkhedronpkg, lint-shell.sh, iso-manifest.py
├── tests/              # 100+ offline tests (stdlib unittest, no network)
└── docs/               # the good stuff
```

## Three commands to trust it

```sh
cd hedronos
make verify      # static whole-system verification: ~40 checks across 400+ files
make test        # unit tests: detection engine, resolver, store index, tuning, ISO manifest
make iso DRY_RUN=1   # full build with side effects stubbed → inspects the tree you'd ship
```

`make verify` is not a formality: it re-generates the GPU database from the kernel
tree next door, fails if any systemd unit names a binary no package provides, if any
profile references a package that exists in neither our catalog nor the Arch repos, if
a `defconfig` symbol is not in any Kconfig, and if any shell file does not parse. If
`verify` and `test` are green, `build.sh` has nothing left to discover at runtime.

## License

Everything in `hedronos/` is **MIT** (see [`LICENSE`](LICENSE)) except the
`../tools/sched_ext/scx_hedron.*` files, which follow the kernel tree's
`GPL-2.0` / `LGPL-2.1` terms, and bundled third-party sources fetched at build time,
which keep their own.
