# hedronos-tune

FlowState: what a claim of "a game is running" is allowed to do to this machine.

The tuning is data, and the daemon is small. `/etc/hedron/flowstate.d/*.toml` holds the layers —
`10-base`, then a power profile, then a vendor or form-factor overlay — and each rule says where to
find its target (a sysfs path, a `cpupower` invocation, a module parameter) and whether it is
revertible. That is what makes the whole thing debuggable: `hedron-tune status` names the file the
last value came from, and `revert` can undo a rule because it recorded what it replaced.

## What installs

    /usr/bin/hedron-tune             list status show apply dry-run transactions revert lock profile
                                     game-on game-off browser-optimize gpu-clocks allow-third-party-power
    /usr/bin/hedron-latency          wake-up latency probe, built from /usr/src/hedron-latency.c by `make`
    /usr/lib/hedron/hedronflowd.py   the responder: reacts to the claim, applies, reverts, logs
    /etc/hedron/flowstate.conf, /etc/hedron/flowstate.d/*.toml
    /usr/lib/systemd/system/hedron-flowstate.service          system-side responder (root-owned knobs)
    /usr/lib/systemd/system/hedron-flowstate-apply@.service   one-shot apply for a user
    /usr/lib/systemd/user/hedron-flowstate-engage@.service    per-session half
    /usr/lib/systemd/user/hedron-game.slice                   where a claimed game's whole tree goes
    /usr/lib/systemd/system/user@.service.d/{delegate,hedron-slice}.conf
    /usr/lib/tmpfiles.d/hedron-tune.conf                      /run/hedron, /var/lib/hedron/tune

## Two knobs we will not turn silently

Anything that can shorten battery life or make a machine hot stays off unless asked for. The
`30-gaming` layer is opt-in through the store or `hedron-tune profile gaming`; the rules that need a
third-party kernel module or an unsigned MSR write are gated behind `allow-third-party-power`, and
`hedron-tune status` prints a warning line naming each disabled rule and why. `hedron-latency -p 16667
-n 20000` is how you check the result instead of trusting the description: it reports p50, p99 and
p99.9 wake-up latency at a 60 Hz cadence, with `--json` for the doctor's benchmark pass.

## Notes for packagers

`hedron-game.slice` and the `user@` drop-ins are the reason a game's Wine prefix, its launcher, and
its overlays all get the same treatment: the claim moves the *process tree*, not one pid. The slice
file lives here rather than in `hedronos-gamemode` so the cgroup exists even on a machine where the
daemon is not running.
