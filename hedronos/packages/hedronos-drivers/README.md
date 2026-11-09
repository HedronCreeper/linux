# hedronos-drivers

Detection, matching rules, and the install order — the whole GPU story in one package.

Nothing here is an answer the kernel did not already give. `hedron-gpu-detect` reads
`lspci -nnv` and `/sys/bus/pci/devices/*/driver`; `hedron-drivers plan` resolves the PCI ids through
`data/driver-rules.toml` and the device database. The rules are data, reviewed on their own, so a
new card is a database entry rather than a code change.

## What installs

    /usr/bin/hedron-drivers        plan apply verify snapshot enroll-mok fix-suspend blacklist-check units
    /usr/bin/hedron-gpu-detect     report — what the machine actually has
    /usr/lib/hedron/driverd.py     the on-apply helper the systemd socket activates
    /usr/lib/systemd/system/hedronos-drivers.service   one-shot reconcile at boot
    /usr/lib/systemd/system/hedronos-driverd.{service,socket}
    /usr/lib/udev/rules.d/85-hedron-graphics.rules     re-runs reconcile on a card appearing
    /usr/lib/initcpio/{hooks,install}/hedronos-drivers  so the module list survives into the initramfs
    /etc/hedron/drivers.conf       policy: allow-proprietary, defer-mok, firmware dir

## The apply sequence, and why it is one sequence

`hedron-drivers apply` does, in this order: snapshot the current module and X config state → install
the packages the plan named → write modprobe黑名单 and regenerate initramfs → rebind the driver on
the live device → run `verify`. Split apart, each step looks optional; together they are the
difference between "the package is installed" and "the desktop came up on the card". `--dry-run`
prints the whole sequence without touching anything, which is the form we suggest in the handbook.

NVIDIA is where distros get this wrong, so the package owns three things explicitly: the suspend
hook (`fix-suspend` writes the sleep script that unloads the module — without it a laptop resumes to
a black screen), the module blacklist (`blacklist-check` reports the open driver fighting the
proprietary one before you reboot into it), and Secure Boot (`enroll-mok` explains and schedules the
MOK enrolment rather than installing a module that cannot load).

## Notes for packagers

The plan format is defined in `tools/hedronlib/drivers.py` (`hedronos-driver-plan/1`) and written at
image-build time by `hedron-gpu-plans.py`, so the ISO carries the rules and the database that produced
it. `hedronos-verify --only drivers` checks those three files still agree.
