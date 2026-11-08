# hedronos-firstsetup — the chooser and the installer

`hedron-setup` is the app chooser: 66 apps, 14 categories, bundles, live size estimate, Steam
locked on. `hedronos-install` is the backend: probe → plan → partition → pacstrap → HedronOS
layer (drivers + boot entries + user) → your apps → verify → unmount.

## Selection contract

`/var/lib/hedron/selected-apps.json` (`hedron-selection-1`): `apps[] {app, selected, name, source}`,
`locked[]`, `counts{}`, `size_mib`, `commands[]`. The installer consumes it, `hstore
check-selection` verifies it was honoured, and `hedron-doctor` reports the diff. One file, three
tools, no way to disagree.

## Testing without a disk

    hedron-setup --print --json                      # catalogue + defaults
    hedron-setup --out /tmp/sel.json --select discord --reset --profile gamerte
    hedronos-install --dry-run --data-dir data --selection /tmp/sel.json --auto /dev/vda

`--dry-run` prints the sfdisk spec, the mount lines, the package list and every chroot command —
which is exactly what `build.sh --dry-run --stage rootfs` rehearses.
