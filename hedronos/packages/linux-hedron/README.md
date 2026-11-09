# linux-hedron — the kernel half of the distro

The kernel is this repository. `hedronos/build.sh --kernel auto` configures and builds the tree
this file lives in, with `hedronos/kernel/hedronos_defconfig`, and this package ships only what a
HedronOS install needs *around* that kernel.

    hedronos/kernel/defconfig-spec.py   860 hand-written intents
    hedronos/kernel/hedronos_defconfig  generated; every symbol verified against this tree
    hedronos/kernel/hedronos.fragment   real-time / immutable overlay, also verified
    hedronos/tools/gen-defconfig.py     the generator (spec -> defconfig, --require to be strict)
    hedronos/tools/kconf-check.py       proves a config invents nothing
    PKGBUILD                            the Arch-style build, for `makepkg -si`

## Why generated

A pasted defconfig decays quietly. In this 7.4 tree `SCHED_DEBUG` is gone, `CPU_IDLE_GOV_TAP`
became `TEO`, `MEMCG_SWAP` folded into `MEMCG`, `RETPOLINE` moved under `MITIGATION_*`, and
`ZPOOL/ZBUD/Z3FOLD` no longer exist as user choices — kbuild drops those lines without complaint,
so the failure shows up a year later as "why is my zswap not backing". The generator looks each
name up in the tree's own Kconfig files; `--require` makes a typo a build error, and `--report`
lists the 27 things we asked for that this kernel declined (all marked `?` in the spec, so vendor
HID/IIO options can come and go without breaking a build).

## What the initramfs hook buys

`hedron-early` puts the detected GPU driver into the initramfs, so the splash, the driver's own
error messages and the desktop are all on one console. It no-ops when a card is already bound —
the hook exists to avoid a late `modprobe`, not to argue with the kernel.
