# hedronos-gamemode

FlowState: a game says it is playing, and the machine stops treating it like a background task.

The protocol is GameMode-compatible, so anything already written for Feral's daemon works unchanged
— which matters more than writing a nicer protocol would. What we add is *liveness*: the claim dies
with the claiming pid, without a timeout. A launcher that is killed or crashes cannot leave the box
permanently tuned for a game that is no longer running, and that single property is why no session
ends up mysteriously loud.

## What installs

    /usr/bin/hgm                  the client and the daemon in one binary (`hgm --daemon` is the unit's ExecStart)
    /usr/bin/gamemoderun          drop-in for scripts that call `gamemoderun ./game`
    /usr/bin/hedron-run           HedronOS-specific launcher: claim, place in hedron-game.slice, exec
    /usr/lib/systemd/user/hedron-gamemoded.service   per-user socket activation
    /usr/src/hedron-gamemode.c    the C source; `make` builds the binary above
    /etc/hedron/gamemode.conf     what a claim is allowed to touch

## Building

`make -C packages/hedronos-gamemode` compiles `hgm` into the package tree with the distro's flag set.
With no compiler the package still builds and installs — the Makefile prints a skip line — so a
source package is never the reason an image fails. `stage_packages` runs the same `make` for every
package that ships a Makefile, before `mkhedronpkg` tars the tree.

## The socket, and why a root service is not enough

The daemon listens on `/run/user/$UID/hedron/gamemode.sock`, one per user, because the things worth
doing for a game — governor, GPU clocks, an input-boost style priority bump — belong to the session
that owns the compositor, not to a system daemon watching other people's games.

```
$ hgm status
$ hgm start $$ my-game
$ hedron-tune status
```

The first two lines are the whole manual check: the claim goes active, and `hedron-tune status` shows
the effects FlowState applied on top of it. If they disagree, `hedron-doctor` reports `gamemode/*`.
