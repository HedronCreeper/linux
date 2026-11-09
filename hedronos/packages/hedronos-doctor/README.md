# hedronos-doctor — the distro checks itself

`hedron-doctor` exists because a gaming distro's failures are quiet: the driver that did not bind,
the initramfs older than the kernel, the profile that never applied, the store index that is 40
days stale. Each finding is one line with a command attached, and `fix` runs them in dependency
order (drivers → kernel → initramfs → boot entries → units → store → tuning).

    hedron-doctor status                     cached one-screen summary
    hedron-doctor scan                       17+ live checks (sysfs, dmesg, units, store, disk)
    hedron-doctor scan --json                the same, for scripts and for the CI gate
    hedron-doctor fix --dry-run              the repairs it *would* run
    hedron-doctor report                     redacted tarball for a bug report
    hedron-doctor bench                      sha256 / disk / sched_ext / wakeup latency numbers
    hedron-doctor verify                     the distro's own file-and-unit promises

## What makes the GPU check different

It compares `hedron-drivers`' reasoning with `/sys/bus/pci/*/driver`. The database says "this id is
an RTX 4090 and `nvidia` should own it"; sysfs says what actually owns it. When those disagree you
get the disagreement plus the command, which is the whole content of a good forum answer.

## `hedron-doctor report` and your privacy

The bundle is assembled from a fixed allow-list (kernel cmdline, uname, os-release, masked mounts,
kernel log tail, unit table, `lspci -nnk`, the doctor JSON, and `/etc/hedron/*`). Nothing under
`$HOME` is read, `credentials=` is masked before the file is ever written, and `report/README.txt`
inside the tarball tells you to check it (`tar tzf`) before uploading.

## Weekly scan

`hedron-doctor-scan.timer` runs a scan Sunday ~04:00 with a 6 h jitter and `Persistent=true`, so a
machine that was off catches up instead of never being checked. It only writes
`/var/lib/hedron/doctor/last-scan.json`; nothing on a timer ever changes system state here.
