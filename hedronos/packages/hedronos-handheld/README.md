# hedronos-handheld — Deck-class machines

`hedron-handheld` reads and writes only the knobs this kernel exposes
(`intel-rapl` constraints, `cpufreq/boost`, `amd_pstate/status`, `amd_pmf.0/cnqf_enable`,
backlight, charge ceiling) and reports "not exposed by this kernel/firmware" instead of
half-applying a mode. `hedron-deck-session` starts the gamescope session and validates every
configured gamescope flag against `gamescope --help` on the machine, dropping unknown ones with
a warning. The suspend hook flushes the replay ring, re-applies the TDP cap for the saved mode,
and fixes the black-panel-on-resume firmware bug.

    hedron-handheld status --json     # what the hardware actually lets us do
    hedron-handheld modes            # 5 modes, each with a plain-language tradeoff
    hedron-handheld mode performance
    hedron-handheld tdp 17           # refuse anything outside 3–250 W
    hedron-handheld session --dry-run
    hedron-deck-session              # gamescope + Steam Big Picture

## Charge thresholds

`charge_control_end_threshold` is a firmware feature, not a kernel one: Lenovo, Dell and some
AYANEO firmware expose it, most gaming handhelds do not (they keep the cell at 100 % on purpose
because they thermally bake a full cell). Where it is missing, `hedron-handheld charge-limit`
says so and exits 1 rather than pretending — the vendor-specific route (EC writes) is listed
here instead of being silently half-implemented.
