# hedronos-store

One catalog, four back ends, and a client that plans before it touches anything.

The catalog is data (`data/apps.toml`, 66 apps) and the resolver is a library (`hedronlib.catalog`);
the CLI, the GUI, the installer, and the first-boot helper all call the same resolver, so "what would
this install" has one answer. No back end is privileged: pacman, Flatpak, AppImage and HedronOS-owned
packages are four providers with one interface, and an app may name more than one.

## What installs

    /usr/bin/hstore                                     search list info categories plan install remove update undo
                                                        status stats journal check-selection verify seed gui
    /usr/lib/hedron/hedrostd.py                         the resolver the CLI talks to, and its in-process fallback
    /usr/lib/systemd/user/hedrostd.{service,socket}     journal + serialisation for concurrent clients
    /etc/hedron/store.toml                              back-end enablement, mirrors, trust policy
    /etc/pacman.d/hedron-os.conf                        signing and parallel downloads for our repository
    /usr/share/hedron/store/Store.qml, /usr/share/applications/hedron-store.desktop,
    /usr/share/metainfo/hedron-store.appdata.xml         the graphical front end and its software-centre entry

## Planning, not installing

```
$ hstore plan steam
$ hstore install steam --plan-only
```

`plan` renders the resolution — the commands each back end will run, the estimated size, the
post-install steps, and what is unresolvable — and `install` refuses to proceed when the plan contains
an unknown reference. `undo` re-runs the journal backwards for the steps that are reversible and says
plainly which ones are not, which is the honest version of a feature distros usually oversell.

## Notes for packagers

`hstore gui` needs `python3-pyqt6`; it is an optional dependency, and with it missing `gui` prints the
package to install and exits 1 rather than dying in an import. `hstore seed` builds the on-disk index
from the catalog; `hedron-drivers apply` and the first-boot pass both call it, so an ISO ships with a
populated index and a machine that has never seen a mirror can still answer "is Steam installed?".
