# Tuning: profiles, game focus, and how to prove it worked

HedronOS ships one tuning tool on purpose. `hedron-tune` is the only thing that writes kernel
scheduling knobs; `hedron-gamemode` (`hgm`) is the only thing that decides *when* those knobs should
change; the session bridge tells us what the user is looking at. If you are hunting for "where is
the switch that makes this box feel fast", it is here, and every switch is reversible.

## Profiles

```
$ hedron-tune list
$ hedron-tune status
$ hedron-tune apply --profile gaming
$ hedron-tune show --profile gaming --diff
```

A profile is a text file in `/usr/lib/hedron/tune/profiles/` — key = value, one knob per line, no
code. `hedron-tune apply` reads the keys, checks each target exists on this machine, writes it, and
records the previous value in `/var/lib/hedron/tune/current-profile/` so it can undo itself:

```
$ hedron-tune revert
```

The profiles that ship: `performance` (desktop, plugged in), `battery` (handheld), `quiet` (fan
noise first), `studio` (isochronous audio and capture), `desktop`, `server`. `hedron-tune apply
--profile quiet` on a machine with a discrete GPU deliberately *does not* touch GPU clocks: fan
curve and clock lock are different promises and only one of them is safe to make unattended.

## Game focus

`hedron-gamemode` is a socket daemon at `/run/user/$UID/hedron/gamemode.sock`. A launcher, a
proton prefix, or you, ask for focus:

```
$ hgm start 41288 steam          # claim focus for a pid, naming the app
$ hgm status                     # who holds a claim right now
$ hgm query                      # am I (this shell's session) covered?
$ hgm end 41288                  # release; also happens when the pid dies
$ hgm ping                       # is the daemon there at all
```

While focus is held:

* `/run/user/$UID/hedron/focus.json` names the focused pid. `scx_hedron` reads it (through its
  loader), FlowState reads it, and the overlay HUD reads it. It is a file, so `cat` answers "does
  the system know I am playing".
* `hedron-tune apply --profile gaming` applies the low-latency subset (governor, `pm` QoS, no deep C-states
  on the game's cores, RCU callbacks offloaded).
* The scheduler gives that pid a longer slice and charges its virtual time at a fraction of the
  rate, so a background compile still finishes; `hedron-tune` never starves anything.

FlowState (`hedron-flowstate-engage@.service`) is the automatic half: it watches session and focus
state and toggles the profile when you enter or leave a game, so nothing has to be remembered.

## sched_ext

The distribution kernel builds with `CONFIG_SCHED_CLASS_EXT=y` and `CONFIG_DEBUG_INFO_BTF=y` and
carries `scx_hedron`. It is not enabled at boot: a scheduler is a policy decision, and a policy
decision that cannot be turned off is a bug report waiting for someone's specific workload.

```
$ hedron-tune scheduler status
$ hedron-tune scheduler on --dry-run
$ hedron-tune scheduler on
```

If the scheduler misbehaves, `scx` breaks out on its own: the kernel takes the tasks back and the
exit reason lands in the journal (`hedron-doctor status` reports it as a `kernel/*` finding). That is
why shipping a custom scheduler is safe in a way that patching EEVDF never was.

## Measuring, instead of hoping

`hedron-doctor bench` runs three probes — a scheduler wake-latency loop, a sequential disk write,
and a crypto throughput check — and prints them next to the last run, so an upgrade that costs you
3 ms of latency is visible rather than deniable:

```
$ hedron-doctor bench
$ hedron-doctor bench --latency-ms 2000
$ hedron-doctor history
```

The kernel side is a tracer, not a guess. With `CONFIG_HWLAT_TRACER` and `CONFIG_TIMERLAT_TRACER`
in the defconfig, `hedron-latency` (in this repo's `tools/`) will tell you the worst single gap the
hardware produced over a minute of idling, which is the number that corresponds to "stutters".
