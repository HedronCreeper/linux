# hedronos-release

The release identity of the image, and the first-boot machinery that acts on it.

Everything in here answers "which HedronOS is this, and what has it done to my machine so far". Both
halves matter: the labels (`/etc/os-release`, `os-release`-adjacent pretty name, product version) and
the record of actions (`/var/lib/hedron/state`, the rollback snapshots, the first-boot journal). A
distro that changes the machine on first boot without a record of it is indistinguishable from a bug.

## What installs

    /etc/os-release, /usr/lib/os-release   the OS identity
    /etc/hedron/release.json               product, version, codename, variant, build id
    /etc/hedron/features.json              which features exist, and how to detect each one
    /etc/dnf/dnf.conf, /etc/pacman.conf    package-manager defaults, incl. parallel downloads
    /usr/bin/hedronctl                     status / features / boot / logs / report
    /usr/bin/hedron-notify                 one notification from system context (no session bus needed)
    /usr/lib/hedron/hedron-firstboot.py      the first-boot pass
    /usr/lib/hedron/hedron-rollback.py       snapshot + rollback around image and driver changes
    /usr/lib/systemd/system/hedron-firstboot.service, hedron-firstapps.{service,timer}
    /usr/lib/systemd/system/hedron-rollback-{autosnap,snap@}.service(+.timer)
    /usr/lib/tmpfiles.d/hedron-release.conf  /var/lib/hedron, /var/log/hedron

## The feature file, honestly

`features.json` is generated from `tools/features.toml` (`make features`), and each entry says how to
detect the feature — a binary, a sysfs path, a kernel config symbol, a package name — not just a
claim. `hedronctl features` renders it against the live machine, and `hedronos-verify --only features`
checks every probe names something we can actually see on the ISO. That is the whole trick the
handbook needs: "you have FlowState" is testable, "we optimise gaming" is not.

## First boot

`hedron-firstboot.py` runs once, gated by a `ConditionPathExists=!/var/lib/hedron/firstboot-done`, so
it cannot fire on a restored snapshot. It applies the driver plan the image carries, runs the store
seed for the profile's default apps, and writes a rollback snapshot before any of it. Every step is
logged and idempotent; a failed pass leaves the machine in a state `hedron-doctor` can read and
repeat, because a first boot that half-completes is the normal case, not the exception.
