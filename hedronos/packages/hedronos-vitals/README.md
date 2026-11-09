# hedronos-vitals

One sampler, every consumer. SPDX-License-Identifier: MIT

Nine tools polling `sensors`, `nvidia-smi`, and debugfs at the same time is how a monitoring stack
ends up costing frames — and a HUD that costs frames is a HUD people turn off. So there is exactly
one reader of the slow interfaces: `hedron-vitalsd`, which samples into a single shared snapshot and
an mmap'd ring buffer, and serves both over a per-user socket. Every front end is a client.

## What installs

    /usr/bin/hedron-vitals                      csv history snapshot toggle watch why-laggy
    /usr/lib/hedron/hedron-vitalsd.py            the daemon (also usable directly by tests and scripts)
    /usr/lib/hedron/overlay/hedron-vitals.qml     the compact HUD shown inside the game
    /usr/lib/systemd/user/hedron-vitalsd.service  starts on login, restarts on failure
    /usr/share/kwin/scripts/hedron-vitals/{main.js,metadata.json}    KWin script: HUD + F10 toggle
    /usr/share/plasma/applets/hedronos.vitals/...                    tray applet, same snapshot
    /etc/hedron/vitals.conf                       intervals, thresholds, which sensors to read
    /usr/lib/tmpfiles.d/hedron-vitals.conf      /run/hedron/vitals

The HUD and the applet render one string, from `overlay/hud-line.txt` in the data directory, so
"what does the box think is happening" cannot mean two things depending on which corner you look at:

    118 fps · p99 9.1 ms · 71°/64° · 84 W

## Why `why-laggy` exists

```
$ hedron-vitals why-laggy
```

It reads the ring and answers in the order a player checks: whether frame pacing degraded, whether
the GPU or the CPU thermal-limited first, whether the game lost its CPU while a background process
was runnable, whether the claim expired mid-session. Those are four different fixes, and the
alternatives — `mpstat`, `nvidia-smi dmon`, a profiler — turn a two-second question into a research
project. It exits 0 whether or not it found anything, because it runs from the KWin script.

## Notes for packagers

`lm_sensors`, `nvidia-utils`, `corectrl` and `jq` are optional: with none installed the daemon samples
frame times and load only, and `snapshot --json` reports which sources are live. No sensor read may
need root, which is the reason `nvidia-smi` is preferred over `nvidia-settings -q` here.
