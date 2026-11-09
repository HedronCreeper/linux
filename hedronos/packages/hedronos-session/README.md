# hedronos-session

Which graphical session this is, and how to start a better one. SPDX-License-Identifier: MIT

A gaming distro's opinion belongs in the session entry, not in a README: if the default Wayland
session already runs at the right scale, with the right cursor settings, nobody has to configure a
thing. This package installs those session entries and the small read-only tool that reports what is
in force, so every other component — overlay, vitals, store, doctor — asks the same question the same
way.

## What installs

    /usr/bin/hedron-session                which / start / bridge / hdr / doctor
    /usr/bin/hedron-session-query          read-only report for scripts and applets
    /usr/lib/hedron/hedron-session-bridge  the XWayland/HDR bridge helper
    /usr/lib/systemd/user/hedron-session-bridge.service
    /usr/share/wayland-sessions/hedron-gamescope.desktop    the desktop-session entry
    /usr/share/wayland-sessions/hedron-deck.desktop         the handheld session (gamescope + Steam)
    /usr/share/xsessions/hedron-x11.desktop                 an explicit X11 fallback
    /etc/hedron/session.conf                scaling, HDR policy, whether XWayland is wanted

`start` prints the command rather than running it — logging out of the session you are typing in is
not a side effect a CLI should have. `which` is the answer `hedron-doctor` and the HUD compare against
when they explain a missing overlay: an overlay cannot appear in a session that has no compositor to
draw it.

## Handheld

`hedron-deck-session` is the session a Deck-class device boots into: gamescope at the panel's native
mode, Steam in Big Picture with the runtime forced off so the host Vulkan drivers are visible to the
game. That last detail is the whole reason the entry exists — with the bundled runtime libraries
active, a handheld gets the runtime's old Mesa while the compositor uses the current one, and the two
disagree about what the GPU can do.
