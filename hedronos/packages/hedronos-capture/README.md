# hedronos-capture

Rewind, and the two ways to get a clip out of it. SPDX-License-Identifier: MIT

`hedron-replayd` keeps a rolling ring of the last few minutes of screen as short, cheap segments,
written to `$XDGVideos/HedronOS/replay-ring`. A normal screen recorder records into a file you have
to review; this records into a buffer you throw away, and only the moment you keep becomes a file.
The daemon is deliberately boring: it shells out to `ffmpeg` with the capture source the compositor
announces, and reaps the ring by count and by age.

## What installs

    /usr/bin/hedron-replay          mark / save / save-around / start / status / stop / trim
    /usr/bin/hedron-share           chat / discord / forum / raw — the same clip, four audiences
    /usr/lib/hedron/hedron-replayd.py   the daemon itself
    /usr/lib/systemd/user/hedron-replayd.service
    /etc/hedron/replay.conf         ring length, segment seconds, target fps
    /etc/xdg/autostart/hedron-replay.desktop  starts the daemon for a desktop session

## Verbs

`save-around` is the one worth learning: it writes the N seconds *before* the keypress as well as
what follows, because the play you want to keep happened while you were still reacting to it.
`hedron-share forum` re-encodes to a size the forums accept, and `hedron-share chat` picks the
smallest file that is still legible — the same source clip, different constraints, no dialog boxes.

## Notes for packagers

`ffmpeg` is a runtime dependency, not optional: without it the daemon starts, says so once in the
journal, and stays out of the way. `hedron-replay status` prints whether the ring is live, which is
what `hedron-doctor` reads when it reports a `capture/*` finding.
