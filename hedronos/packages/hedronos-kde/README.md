# hedronos-kde — the KDE Plasma layer, kept deliberately thin

Two colour schemes (Dark + Light, both generated from `branding.json`), five defaults files, an
applier that merges per key instead of replacing your config, an SDDM theme, and one KWin script
for the desktop-facing keys. The compositor-facing tuning (game slice, FlowState, gamemode) lives
in `hedronos-tune`/`hedronos-gamemode`, so a GNOME user gets the same behaviour — this package is
presentation, not policy.

    hedron-kde-apply apply --verbose   # merge defaults into ~/.config (backs up what it touches)
    hedron-kde-apply apply --keep-mine # only fill keys you have not set yourself
    hedron-kde-apply check             # exit 1 + a diff when your config drifted
    hedron-kde-apply revert            # put the previous ~/.config files back

## Why an applier instead of /etc/skel or patched defaults

`/etc/skel` never reaches an existing user, and patched system defaults are invisible to System
Settings' "Revert to defaults" — the user sees the distro fighting them. Merging into `~/.config`
per key means: your tweaks survive (0 keys written on a second run), `check` proves the promise,
and `revert` is one command because every touched file was copied to
`~/.config/hedron/kde-backup/<timestamp>/` first.

## KWin scripts

`hedron-keys` (Meta+O next preset, Meta+H HDR, brightness keys) is loaded via D-Bus with the
method name *discovered from KWin itself* (`startScriptForFile` / `loadScript` / `startScript`),
because that interface has moved twice. If nothing matches, it prints the System Settings path
instead of failing the login. The vitals keys live with `hedronos-vitals`; the HUD itself is a
Plasma applet reading the same `/run/hedron/vitals.json` the CLI prints — one sampler, many
readers.

## Not included

No window-decoration or full look-and-feel package: those are SVG/QML bundles with a build step
and a `plugin.json` contract we would be guessing at. Borderless maximized windows, zero
animation time and click-to-focus in `kwinrc` get 90 % of the perceived difference.
