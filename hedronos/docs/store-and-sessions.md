# Apps, sessions, and what the desktop does about them

## hstore: one front end, four back ends

```
$ hstore status
$ hstore search shooter
$ hstore plan steam
$ hstore install steam --plan-only
$ hstore verify
```

`hstore` is a thin client. The work happens in `hedrostd` (`/usr/lib/hedron/hedrostd.py`, socket at
`/run/hedron/store/sock`), which owns the selection, the back-end preference order, and the
signature state of the index. Back ends: `pacman` (preferred), `flatpak`, `appimage`, `hedron`
(our own repo), `aur` (only when the app is marked `source = "aur-allowed"`, and never silently —
`hstore plan` prints the source it chose and why).

Two decisions worth knowing about:

* **Verified vs community.** `data/apps.toml` carries a tier per app. `community` is installable but
  is labelled in the picker and never auto-selected by first-run setup. Locked entries (Steam) are
  not installable through the store at all: the native launcher has to own its own updates, and a
  distro that wraps it in a sandbox gets a support queue full of anti-cheat failures.
* **The plan is not the install.** `hstore plan --json` gives machine-readable package lists,
  sizes, and post-install steps. `hedron-setup` renders exactly that payload, which is why the
  installer and the store cannot offer different things for the same app.

Selections live in one file, `/var/lib/hedron/selected-apps.json`, which is what the installer reads
and what you copy to a new machine before running setup there:

```
$ hstore status
$ hstore check-selection
```

`hedron-setup` renders the same selection into its plan, so "what I picked in the store" and "what
the installer will install" are the same list, from the same file.

## Sessions

```
$ hedron-session which
$ hedron-session doctor
$ hedron-session start plasma
$ hedron-deck-session --dry-run
```

`hedron-session` is the layer that knows about gamescope, Wayland vs X11, and the fact that a
handheld boots into a compositor with no desktop panel. `hedron-session-query` is the read-only half
that HUDs and the store ask; the mutator needs a real session.

`hedron-handheld` owns the Deck-class behaviour: the four power modes, TDP and boost, charge limit,
and the guide-button remap. It refuses to run if the machine has no powercap zone and says which
path it looked for — a tuning tool that silently no-ops on a machine without the interface is worse
than one that errors.

## Overlays and the HUD

```
$ hedron-overlay list
$ hedron-overlay next
$ hedron-vitals toggle
$ hedron-replay start
$ hedron-share --target raw clip.mkv
```

The overlay stack is MangoHud + GOverlay + our own KWin scripts; `hedron-overlay` switches the
*set* of enabled overlays (base, competitive, handheld, minimal, streamer) by toggling drop-in
config in `/etc/hedron/overlay.d/`. `hedron-vitals toggle` writes
`$XDG_RUNTIME_DIR/hedron/hud.enabled`, which is the one file the Plasma applet and the KWin script
watch — no D-Bus round trip inside a frame.

Replay (`hedron-replay start`, `hedron-replay save`) is the instant-replay buffer: a shadow of the last N seconds of
frame-correct GPU work, kept in a ring of encoded segments, saved on demand. It is off by default
on battery, and `hedron-replay status` prints the real memory cost on this machine rather than a
marketing number.
