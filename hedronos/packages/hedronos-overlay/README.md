# hedronos-overlay

Preset files, and the compositor hook that reads them. SPDX-License-Identifier: MIT

An overlay is not a desktop application. It has to start *before* the game, in the game's own
environment, because the alternative — a HUD that needs a window manager to launch — is a HUD that
does not work in fullscreen. So this package installs no panel: it installs data plus a tiny
`environment.d` drop-in, and lets Mangohud, gamescope, or the Steam layer do the drawing.

## What installs

    /usr/bin/hedron-overlay               list / show / use / next / new / edit / export / game
    /etc/hedron/overlay.d/*.conf            presets: 00-base, competitive, handheld, minimal, streamer
    /etc/hedron/overlay.conf               which preset is active, and the per-app table
    /usr/lib/systemd/user/hedron-overlay-apply.service   re-applies the drop-in when the choice changes
    /usr/lib/environment.d/60-hedron-overlay.conf        MANGOHUD_CONFIGFILE / ENABLE_VULKAN_LAYER / gamescope vars

A preset is key-value data with `[hud]`, `[graph]`, `[colour]` and `[apps]` tables, generated from
`data/overlay-presets.toml` (`python3 -m hedronlib.tune presets --check` re-renders and diffs them).
`streamer` is the exception: it exists because OBS is the one thing that must *not* be placed in
`hedron-game.slice`, and a preset that keeps the encoder out of the game cgroup is the difference
between 60 fps in the recording and 60 fps on the monitor.

## Verbs you will actually use

`use` picks a preset, `next` cycles, `game <name> <preset>` pins one preset to one app, and `show`
prints what is in effect right now — including which file the last key came from, since a drop-in and
a preset can say different things and the game only obeys one. `edit` opens the preset in
`$EDITOR` and validates it on exit; `export` renders the same settings as plain shell variables, for
a launcher that will not read our drop-in.

## Notes for packagers

Mangohud and gamescope are `optional` dependencies, and the package works without either: `list` and
`show` still answer, `hedron-doctor` explains what is missing, and no session file references a layer
that is not installed.
